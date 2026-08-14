# Copyright 2026 Nikolay Petrov
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Architecture review P1-1: make the abicheck Clang plugin's
`source_facts/*.jsonl` output a real, declared Bazel action output instead
of an untracked side effect of an ordinary `cc_binary`/`cc_library` compile
action.

Before this file existed, `.github/workflows/abi-scan.yml`'s `l4_clang_plugin`
job injected the plugin via `--copt="-fplugin=..."` on a normal `bazel build
//:math` -- from Bazel's own point of view that compile action's only output
is `math.pic.o` (or equivalent); the plugin's `source_facts/*.jsonl` write is
invisible to Bazel's action graph entirely. Bazel's caching contract (local
action cache, disk cache, remote cache) is "a cache hit downloads/restores
the action's *declared* outputs instead of re-running it" -- since the facts
file was never a declared output, a cache hit on that exact compile action
(same copts/toolchain) would silently skip invoking clang and therefore the
plugin, leaving the facts directory empty while the build itself still
reported success. The workflow's own fix at the time was to disable this
job's disk cache entirely (see its own comment on the "Deliberately NO
disk-cache step" step) -- a workaround, not a fix for the underlying
declared-output gap.

The fix here is structural, not another workaround: `abicheck_facts_aspect`
runs the plugin as its OWN action, entirely separate from the target's
normal compile action, with the resulting `source_facts/` directory declared
as that action's output via `ctx.actions.declare_directory`. Once an output
is declared, Bazel's ordinary caching machinery applies to it like any other
action output -- a cache hit now correctly restores the facts directory
(the action simply doesn't re-run, and its declared output is fetched from
whichever cache produced it, same as an object file would be), and the
scan/canary jobs' own disk-cache step (present on every other job in this
workflow) is safe to enable here too.

Design notes:
- This is a SEPARATE action from the target's real compile action, not a
  modification of it -- `cc_common.compile()`'s public API has no hook for
  an arbitrary extra declared output per translation unit, so reusing the
  real compile action directly isn't available. Running the compiler a
  second time with `-fsyntax-only` (verified empirically against a real
  built plugin: the plugin is a `PluginASTAction` with `AddAfterMainAction`,
  an ordinary ASTConsumer that fires during Sema regardless of whether
  codegen happens, so `-fsyntax-only` triggers it and produces
  byte-identical source_facts to a real `-c` compile) means this doesn't
  need to also produce or declare a competing object file.
- The command line is derived from the SAME `cc_common` APIs
  (`create_compile_variables` / `get_memory_inefficient_command_line`) a
  real compile action would use, built from the target's own `CcInfo`
  compilation context -- so the facts-collecting invocation sees the exact
  include/define/copt set the real compile does, not a hand-approximated
  subset.
- `public-roots=` is derived from the compilation context's own
  `system_includes` (what a `cc_library`'s `includes = [...]` attribute --
  the mechanism this repo's own `:math_api` target uses for its public
  header directory -- lowers to, always as `-isystem`). The plugin's own
  auto-derivation (omitting `public-roots=` entirely) explicitly excludes
  `-isystem` directories (per contrib/abicheck-clang-plugin/README.md), so
  relying on it here would silently produce an EMPTY public surface for
  this repo's own BUILD.bazel shape -- passing `public-roots=` explicitly
  is required, not optional polish.

  That alone is NOT sufficient, though (verified empirically: it still
  produced zero declarations even with an explicit, correctly-matching
  `public-roots=`). The plugin gates every declaration on
  `SourceManager::isInSystemHeader()` BEFORE ever testing it against
  `public-roots` at all (see `AbicheckFactsPlugin.cpp`'s own
  `isInSystemHeader` call sites) -- and `-isystem` is exactly the compiler
  flag that marks a directory's headers as system headers to Clang itself,
  regardless of what `public-roots=` says about them. So this aspect
  builds the facts-collection command line with ONLY the `public_roots`
  subset of `system_includes` folded into `include_directories` (rendered
  as `-I`) INSTEAD of `system_include_directories` (`-isystem`) -- for
  this one, separate fact-collecting action only, never for the target's
  real compile action, which keeps its ordinary `-isystem` semantics
  untouched. Every OTHER `system_includes` root -- one that didn't back
  a direct public header, e.g. a third-party/transitive dependency's own
  `-isystem` root -- still reaches this action as a real `-isystem` entry
  (`system_include_directories`, not emptied outright): dropping it
  entirely would leave a TU that legitimately `#include`s something from
  that root unable to resolve it in this facts-only action even though
  the real compile resolves it fine, silently skipping the whole facts
  action (Codex review, fresh evidence). `public_roots` itself still
  names exactly the same directories either way, so this reclassification
  only removes the system-header disqualification for directories the
  caller already intends as public roots -- it cannot broaden the public
  surface to some other, unrelated `-isystem` directory that isn't also
  named in `public_roots`.
"""

load("@rules_cc//cc:action_names.bzl", "CPP_COMPILE_ACTION_NAME")
load("@rules_cc//cc:find_cc_toolchain.bzl", "find_cpp_toolchain", "use_cc_toolchain")

_SOURCE_EXTENSIONS = ("c", "cc", "cpp", "cxx", "c++", "C", "CC", "cu")

def _is_source_file(f):
    return f.extension in _SOURCE_EXTENSIONS

def _public_roots_for(ctx, compilation_context):
    """The subset of `compilation_context.system_includes` that actually
    backs at least one DIRECT public header of this target or one of its
    direct `deps` -- not the full transitively-merged list.

    `system_includes` is a fully-merged, transitive view: it accumulates
    every `-isystem` root any dependency anywhere in the closure
    contributed, including one a completely unrelated, several-hops-away
    library exports for its OWN `includes = [...]` attribute (Codex
    review, fresh evidence). Blindly promoting the whole list to
    `public-roots=` (see the module docstring's own reasoning for why
    `-isystem` roots need promoting to `-I` at all) would classify that
    unrelated dependency's own private/third-party declarations as part
    of THIS library's public surface too.

    `compilation_context.direct_public_headers` is the corrective: it's
    scoped to exactly the headers a `hdrs` attribute directly declares
    (not inherited from deps), so cross-referencing which `system_includes`
    entry is actually a path prefix of one of those files' own paths
    recovers "the root this target's OWN public API lives under" instead
    of trusting the merged list wholesale. One hop is taken beyond the
    visited target itself -- direct `deps`' own `direct_public_headers` --
    covering the common "binary depends directly on its own API library"
    shape (exactly this repo's own `:math`/`:math_api` relationship,
    where the compiled `cc_binary` itself declares no `hdrs` at all, so
    its own `direct_public_headers` is always empty and the real answer
    lives one hop up). A public header re-exported through a longer
    facade-library chain is not covered by this narrower, verifiable
    check; documented as a known limitation rather than guessed at.
    """
    direct_header_paths = [f.path for f in compilation_context.direct_public_headers]
    if hasattr(ctx.rule.attr, "deps"):
        for dep in ctx.rule.attr.deps:
            if CcInfo in dep:
                direct_header_paths.extend(
                    [f.path for f in dep[CcInfo].compilation_context.direct_public_headers],
                )
    return [
        root
        for root in compilation_context.system_includes.to_list()
        if any([p.startswith(root + "/") for p in direct_header_paths])
    ]

def _dep_facts(ctx):
    """Every `abicheck_facts` OutputGroupInfo depset already produced by this
    aspect on `ctx.rule.attr.deps` (Codex review, fresh evidence, confirmed
    against a real target: `attr_aspects = ["deps"]` alone only makes the
    aspect *run* on each dep -- it does NOT automatically fold a dep's own
    returned providers into the *visiting* target's. Without this, a target
    with real compiled `deps` -- e.g. a `cc_binary` linking a `cc_library`
    that itself has `.cc` sources, unlike this repo's own header-only
    `:math_api` -- would silently drop every dependency TU's facts from
    the merged pack `abicheck_facts_pack` produces, despite this file's own
    docstring claiming "across the target and its deps").
    """
    if not hasattr(ctx.rule.attr, "deps"):
        return []
    return [
        dep[OutputGroupInfo].abicheck_facts
        for dep in ctx.rule.attr.deps
        if OutputGroupInfo in dep and hasattr(dep[OutputGroupInfo], "abicheck_facts")
    ]

def _abicheck_facts_aspect_impl(target, ctx):
    # Only real compiled C/C++ targets are interesting -- a header-only
    # cc_library (e.g. this repo's own :math_api) has no compile action to
    # shadow, and any non-CcInfo target (a filegroup, a genrule, ...) can't
    # supply the compilation context this aspect needs. Either way, still
    # fold in whatever the aspect already collected from deps -- a
    # header-only or non-CcInfo target can sit *between* two real compiled
    # targets in a deps chain (e.g. an alias, or a wrapping cc_library),
    # and dropping the depset here would silently truncate the chain.
    if CcInfo not in target:
        return [OutputGroupInfo(abicheck_facts = depset(transitive = _dep_facts(ctx)))]
    if not hasattr(ctx.rule.attr, "srcs"):
        return [OutputGroupInfo(abicheck_facts = depset(transitive = _dep_facts(ctx)))]
    srcs = [f for f in ctx.rule.files.srcs if _is_source_file(f)]
    if not srcs:
        return [OutputGroupInfo(abicheck_facts = depset(transitive = _dep_facts(ctx)))]

    plugin_so = ctx.file._plugin_so

    cc_toolchain = find_cpp_toolchain(ctx)
    feature_configuration = cc_common.configure_features(
        ctx = ctx,
        cc_toolchain = cc_toolchain,
        requested_features = ctx.features,
        unsupported_features = ctx.disabled_features,
    )
    compilation_context = target[CcInfo].compilation_context

    # Validated -isystem roots (see _public_roots_for's own docstring --
    # Codex review, fresh evidence: the full, transitively-merged
    # system_includes list can carry an unrelated dependency's own
    # -isystem root that has nothing to do with THIS library's public
    # surface).
    public_roots = _public_roots_for(ctx, compilation_context)

    # Codex review, fresh evidence: the rest of system_includes -- every
    # root NOT promoted to public_roots above -- still needs to reach this
    # action as a real -isystem entry. A TU that #includes a header from a
    # third-party/transitive dependency whose own root isn't one of this
    # target's public roots gets that root via -isystem in the real
    # compile; unconditionally dropping system_include_directories to an
    # empty depset here (rather than just OMITTING the promoted subset)
    # would make such a header unresolvable in this facts-only action even
    # though the real compile resolves it fine -- silently skipping the
    # declared-output facts action and the best-effort L4 plugin profile
    # for any target with this shape, not a correctness gap in what gets
    # classified public/private (that part is public_roots' own job).
    non_public_system_includes = depset([
        root
        for root in compilation_context.system_includes.to_list()
        if root not in public_roots
    ])

    toolchain_inputs = depset(
        transitive = [cc_toolchain.all_files],
    )

    # Codex review, fresh evidence: --cxxopt/--copt/--conlyopt (workspace
    # .bazelrc entries like this repo's own `build --cxxopt=-std=c++17`, or
    # a command-line override) are exposed on the cpp configuration
    # fragment, NOT on the rule's own `copts` attribute -- the previous
    # `user_compile_flags = ctx.rule.attr.copts` fed this action only the
    # rule-local flags, silently dropping every workspace-level one. A
    # header that branches on __cplusplus/the resolved standard, or any
    # future -D/-U passed via --copt, would then be parsed under a
    # different effective configuration than the real compile that
    # produced the binary this facts pack is meant to describe -- exactly
    # the class of divergence the L4 replay-vs-plugin conformance report
    # exists to catch, not manufacture. `--copt` applies to both C and C++
    # compiles; `--cxxopt`/`--conlyopt` apply to only one, so the split
    # below picks the matching one per source rather than always using
    # cxxopts (which would wrongly inject a C++-only flag into a `.c`
    # compile). Order matches a real compile action: workspace-level flags
    # first, rule-local `copts` last, so a rule's own override still wins.

    facts_dirs = []
    for src in srcs:
        # `.c` (lowercase) is the one extension this aspect treats as C
        # rather than C++ -- matching gcc/clang's own suffix convention (a
        # capital `.C` is C++, same as `.cc`/`.cpp`/...), and the only
        # split that matters for choosing cxxopts vs conlyopts below.
        is_c_source = src.extension == "c"
        source_specific_opts = (
            ctx.fragments.cpp.conlyopts if is_c_source else ctx.fragments.cpp.cxxopts
        )

        # src.short_path (the full package-relative path), not
        # src.basename, and NOT a flattened "/" -> "_" encoding of it
        # either -- that flattening aliased two distinct valid paths onto
        # the same declared directory (e.g. "a/b.cc" and "a_b.cc" both
        # produced "a_b.cc"), which Bazel rejects as a duplicate output at
        # analysis time exactly like the bare-basename collision this was
        # originally written to fix (Codex review, fresh evidence).
        # declare_directory accepts a path containing "/" directly -- it
        # creates the intermediate directories as part of the declared
        # tree artifact, so preserving the real relative path is both
        # simpler and, unlike string-munging a separator, guaranteed
        # injective (two different paths are never equal as paths).
        out_dir = ctx.actions.declare_directory(
            "{}.abicheck_facts/{}".format(
                ctx.rule.attr.name,
                src.short_path,
            ),
        )

        compile_variables = cc_common.create_compile_variables(
            feature_configuration = feature_configuration,
            cc_toolchain = cc_toolchain,
            source_file = src.path,
            output_file = out_dir.path + "/unused.o",
            # system_includes folded into include_directories (-I, not
            # -isystem) for THIS action only -- see the module docstring's
            # "That alone is NOT sufficient" paragraph for why: the plugin
            # refuses to classify anything under an -isystem directory as
            # public, no matter what public-roots says.
            include_directories = depset(
                compilation_context.includes.to_list() + public_roots,
            ),
            quote_include_directories = compilation_context.quote_includes,
            system_include_directories = non_public_system_includes,
            framework_include_directories = compilation_context.framework_includes,
            preprocessor_defines = depset(
                compilation_context.defines.to_list() +
                compilation_context.local_defines.to_list(),
            ),
            user_compile_flags = (
                ctx.fragments.cpp.copts +
                source_specific_opts +
                (ctx.rule.attr.copts if hasattr(ctx.rule.attr, "copts") else [])
            ),
        )
        command_line = cc_common.get_memory_inefficient_command_line(
            feature_configuration = feature_configuration,
            action_name = CPP_COMPILE_ACTION_NAME,
            variables = compile_variables,
        )
        env = cc_common.get_environment_variables(
            feature_configuration = feature_configuration,
            action_name = CPP_COMPILE_ACTION_NAME,
            variables = compile_variables,
        )

        args = ctx.actions.args()
        args.add_all(command_line)
        # -fsyntax-only: see the module docstring -- the plugin's
        # ASTConsumer fires during Sema, so no codegen/object file is
        # needed just to collect facts, and this avoids a second action
        # racing the real one to produce (and declare) a competing .o.
        args.add("-fsyntax-only")
        args.add("-fplugin=" + plugin_so.path)
        args.add("-Xclang")
        args.add("-plugin-arg-abicheck-facts")
        args.add("-Xclang")
        args.add("out=" + out_dir.path)
        for root in public_roots:
            args.add("-Xclang")
            args.add("-plugin-arg-abicheck-facts")
            args.add("-Xclang")
            args.add("public-roots=" + root)

        inputs = depset(
            direct = [src, plugin_so],
            transitive = [compilation_context.headers, toolchain_inputs],
        )

        ctx.actions.run(
            executable = cc_toolchain.compiler_executable,
            arguments = [args],
            inputs = inputs,
            outputs = [out_dir],
            env = env,
            mnemonic = "AbicheckFacts",
            progress_message = "Collecting abicheck source facts for %s" % src.short_path,
            use_default_shell_env = True,
        )
        facts_dirs.append(out_dir)

    return [OutputGroupInfo(abicheck_facts = depset(facts_dirs, transitive = _dep_facts(ctx)))]

abicheck_facts_aspect = aspect(
    implementation = _abicheck_facts_aspect_impl,
    attr_aspects = ["deps"],
    attrs = {
        "_plugin_so": attr.label(
            default = Label("//tools/abicheck:plugin_so"),
            allow_single_file = True,
        ),
    },
    fragments = ["cpp"],
    toolchains = use_cc_toolchain(),
)

def _abicheck_facts_pack_impl(ctx):
    """Merges every per-source-file `source_facts/` directory the aspect
    declared (across the target and its `deps`) into ONE `abicheck_inputs/`
    pack directory -- the shape `abicheck compare --build-info <dir>`
    expects (one `manifest.json` + one `source_facts/` directory of
    per-TU `.jsonl` files).

    Per-TU filenames already embed a source-content hash (ADR-038 C.2:
    "a per-TU, race-free filename so parallel -j compiles never share a
    file" -- verified empirically above), so merging many per-source
    directories' `source_facts/*.jsonl` files into one flat directory is
    safe: no two TUs can collide on a filename unless they really are the
    same content. `manifest.json`'s own shape (verified empirically) is
    static/target-independent aside from its `source_facts` directory
    listing, which is always the fixed `["source_facts"]`, so copying any
    ONE constituent action's manifest.json forward is sufficient -- there
    is nothing target-specific inside it to merge.
    """
    facts_dirs = ctx.attr.target[OutputGroupInfo].abicheck_facts.to_list()
    out_dir = ctx.actions.declare_directory(ctx.label.name)

    args = ctx.actions.args()
    args.add(out_dir.path)
    # expand_directories = False: each entry in facts_dirs is itself a
    # TreeArtifact directory (declare_directory) -- Args.add_all's default
    # (True) silently flattens each one to its individual file paths
    # instead, which broke the merge script's own directory-shaped
    # argv contract (verified empirically: the merge action's command
    # line showed manifest.json/*.jsonl paths directly, not their parent
    # directory, before this fix).
    args.add_all(facts_dirs, expand_directories = False)

    # Runs the merge script directly off its own shebang rather than
    # through an sh_binary/py_binary wrapper -- this repo has no Python
    # toolchain registered via bzlmod (only rules_cc), and a source-tree
    # script executed as a plain declared input (with its executable bit
    # preserved, same as any other checked-in tool script Bazel actions
    # invoke this way) needs neither runfiles nor an extra rule.
    ctx.actions.run(
        executable = ctx.file._merge_tool,
        arguments = [args],
        inputs = facts_dirs + [ctx.file._merge_tool],
        outputs = [out_dir],
        mnemonic = "AbicheckFactsMerge",
        progress_message = "Merging abicheck source facts into %s" % out_dir.short_path,
    )

    return [DefaultInfo(files = depset([out_dir]))]

abicheck_facts_pack = rule(
    implementation = _abicheck_facts_pack_impl,
    attrs = {
        "target": attr.label(aspects = [abicheck_facts_aspect], mandatory = True),
        "_merge_tool": attr.label(
            default = Label("//scripts:merge_abicheck_facts.py"),
            allow_single_file = True,
        ),
    },
)

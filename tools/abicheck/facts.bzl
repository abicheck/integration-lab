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
  builds the facts-collection command line with `system_includes` folded
  into `include_directories` (rendered as `-I`) INSTEAD of
  `system_include_directories` (`-isystem`) -- for this one, separate
  fact-collecting action only, never for the target's real compile action,
  which keeps its ordinary `-isystem` semantics untouched. `public_roots`
  itself still names exactly the same directories either way, so this
  reclassification only removes the system-header disqualification for
  directories the caller already intends as public roots -- it cannot
  broaden the public surface to some other, unrelated `-isystem` directory
  (a genuine third-party SDK/toolchain include) that isn't also named in
  `public_roots`.
"""

load("@rules_cc//cc:action_names.bzl", "CPP_COMPILE_ACTION_NAME")
load("@rules_cc//cc:find_cc_toolchain.bzl", "find_cpp_toolchain", "use_cc_toolchain")

_SOURCE_EXTENSIONS = ("c", "cc", "cpp", "cxx", "C", "CC", "cu")

def _is_source_file(f):
    return f.extension in _SOURCE_EXTENSIONS

def _abicheck_facts_aspect_impl(target, ctx):
    # Only real compiled C/C++ targets are interesting -- a header-only
    # cc_library (e.g. this repo's own :math_api) has no compile action to
    # shadow, and any non-CcInfo target (a filegroup, a genrule, ...) can't
    # supply the compilation context this aspect needs.
    if CcInfo not in target:
        return [OutputGroupInfo(abicheck_facts = depset())]
    if not hasattr(ctx.rule.attr, "srcs"):
        return [OutputGroupInfo(abicheck_facts = depset())]
    srcs = [f for f in ctx.rule.files.srcs if _is_source_file(f)]
    if not srcs:
        return [OutputGroupInfo(abicheck_facts = depset())]

    plugin_so = ctx.file._plugin_so

    cc_toolchain = find_cpp_toolchain(ctx)
    feature_configuration = cc_common.configure_features(
        ctx = ctx,
        cc_toolchain = cc_toolchain,
        requested_features = ctx.features,
        unsupported_features = ctx.disabled_features,
    )
    compilation_context = target[CcInfo].compilation_context

    # The same directories the real compile resolves `-isystem` from --
    # see the module docstring for why this (not the plugin's own
    # auto-derivation) is the correct public-surface source for this repo.
    public_roots = compilation_context.system_includes.to_list()

    toolchain_inputs = depset(
        transitive = [cc_toolchain.all_files],
    )

    facts_dirs = []
    for src in srcs:
        out_dir = ctx.actions.declare_directory(
            "{}.abicheck_facts/{}".format(ctx.rule.attr.name, src.basename),
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
            system_include_directories = depset(),
            framework_include_directories = compilation_context.framework_includes,
            preprocessor_defines = depset(
                compilation_context.defines.to_list() +
                compilation_context.local_defines.to_list(),
            ),
            user_compile_flags = (
                ctx.rule.attr.copts if hasattr(ctx.rule.attr, "copts") else []
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

    return [OutputGroupInfo(abicheck_facts = depset(facts_dirs))]

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

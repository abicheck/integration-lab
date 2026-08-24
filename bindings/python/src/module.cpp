#include <pybind11/pybind11.h>

namespace py = pybind11;

PYBIND11_MODULE(_core, module) {
  module.def(
      "transform",
      [](py::bytes data, const std::string& encoding) {
        (void)encoding;
        return data;
      },
      py::arg("data"), py::kw_only(), py::arg("encoding") = "utf-8");
}

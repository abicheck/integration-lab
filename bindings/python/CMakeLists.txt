cmake_minimum_required(VERSION 3.20)
project(abicheck_lab_py LANGUAGES CXX)
find_package(pybind11 CONFIG REQUIRED)
pybind11_add_module(_core src/module.cpp)
install(TARGETS _core LIBRARY DESTINATION abicheck_lab_py)
install(FILES abicheck_lab_py/_core.pyi abicheck_lab_py/py.typed
        DESTINATION abicheck_lab_py)

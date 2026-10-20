"""Explicit import names and runtime artifact roots for the fixed Lite repos."""

PROJECTS = {
    "astropy/astropy": ("astropy", ("astropy",), ("astropy.egg-info",)),
    "django/django": ("django", ("django",), ("Django.egg-info",)),
    "matplotlib/matplotlib": (
        "matplotlib",
        ("lib/matplotlib",),
        ("lib/matplotlib.egg-info",),
    ),
    "mwaskom/seaborn": ("seaborn", ("seaborn",), ("seaborn.egg-info",)),
    "pallets/flask": (
        "flask",
        ("flask", "src/flask"),
        ("Flask.egg-info", "src/Flask.egg-info"),
    ),
    "psf/requests": ("requests", ("requests",), ("requests.egg-info",)),
    "pydata/xarray": ("xarray", ("xarray",), ("xarray.egg-info",)),
    "pylint-dev/pylint": ("pylint", ("pylint",), ("pylint.egg-info",)),
    "pytest-dev/pytest": (
        "pytest",
        ("_pytest", "src/_pytest", "pytest", "src/pytest"),
        ("pytest.egg-info", "src/pytest.egg-info"),
    ),
    "scikit-learn/scikit-learn": ("sklearn", ("sklearn",), ("scikit_learn.egg-info",)),
    "sphinx-doc/sphinx": ("sphinx", ("sphinx",), ("Sphinx.egg-info",)),
    "sympy/sympy": ("sympy", ("sympy",), ("sympy.egg-info",)),
}

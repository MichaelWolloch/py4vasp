from contextlib import contextmanager, nullcontext
from IPython.core.formatters import DisplayFormatter
from IPython.lib.pretty import pretty
from typing import NamedTuple, Iterable
from py4vasp.raw import RawVersion
import py4vasp.exceptions as exception
import py4vasp.raw as raw
import numpy as np
import functools
import numbers


def from_file_doc(doc, filename=raw.File.default_filename):
    return f"""Read the {doc} from the given file.

        Parameters
        ----------
        file : str or raw.File
            Filename from which the data is extracted, using {filename} if not present.
            Alternatively, you can open the file yourself and pass the `File`
            object. In that case, you need to take care the file is properly
            closed again and be aware the generated instance of this class
            becomes unusable after the file is closed.

        Yields
        ------
        contextmanager
            The returned context manager handles opening and closing the file.
            If a `File` object is passed a `nullcontext` is returned.
        """


@contextmanager
def from_file(cls, file, attr):
    if file is None or isinstance(file, str):
        context = raw.File(file)
    else:
        context = nullcontext(file)
    with context as file:
        yield cls(getattr(file, attr)())


def raise_error_if_data_is_none(obj, error_message):
    if obj is None:
        raise exception.NoData(error_message)


def raise_error_if_not_string(test_if_string, error_message):
    if test_if_string.__class__ != str:
        raise exception.IncorrectUsage(error_message)


def raise_error_if_not_number(test_if_number, error_message):
    if not isinstance(test_if_number, numbers.Number):
        raise exception.IncorrectUsage(error_message)


def add_doc(doc):
    def add_doc_to_func(func):
        func.__doc__ = doc
        return func

    return add_doc_to_func


def decode_if_possible(string):
    try:
        return string.decode()
    except (UnicodeDecodeError, AttributeError):
        return string


default_selection = "*"


class Selection(NamedTuple):
    "Helper class specifying which indices to extract their label."
    indices: Iterable[int]
    "Indices from which the specified quantity is read."
    label: str = ""
    "Label identifying the quantity."


def add_specific_wrappers(specific_wrappers={}):
    default_wrappers = {"read": "to_dict", "plot": "to_plotly"}
    actual_wrappers = {**default_wrappers, **specific_wrappers}

    def add_wrappers_decorator(cls):
        for wrapping, wrapped in actual_wrappers.items():
            if hasattr(cls, wrapped):
                setattr(cls, wrapping, _make_wrapper(cls, wrapped))
        return cls

    return add_wrappers_decorator


def _make_wrapper(cls, wrap_this_func):
    @functools.wraps(getattr(cls, wrap_this_func))
    def wrapper(self, *args, **kwargs):
        this_func = getattr(self, wrap_this_func)
        return this_func(*args, **kwargs)

    return wrapper


add_wrappers = add_specific_wrappers()

format_ = DisplayFormatter().format
_minimal_vasp_version = RawVersion(6, 2)


class DataMeta(type):
    def _repr_pretty_(cls, *args, **kwargs):
        with cls.from_file() as obj:
            obj._repr_pretty_(*args, **kwargs)

    def _repr_mimebundle_(cls, *args, **kwargs):
        with cls.from_file() as obj:
            return format_(obj, *args, **kwargs)

    def __str__(cls):
        return pretty(cls)


class Data(metaclass=DataMeta):
    def __init__(self, raw_data):
        if raw_data.version < _minimal_vasp_version:
            raise exception.OutdatedVaspVersion(
                "To use py4vasp features, you need at least Vasp version "
                f"{_minimal_vasp_version.major}.{_minimal_vasp_version.minor}."
                f"{_minimal_vasp_version.patch}. The used version is "
                f"{raw_data.version.major}.{raw_data.version.minor}."
                f"{raw_data.version.patch}. Please use a newer version of Vasp."
            )
        self._raw = raw_data

    def __str__(self):
        return pretty(self)

    def __repr__(self):
        return f"{self.__class__.__name__}({repr(self._raw)})"


def require(version, err_msg=None):
    def decorator_require(func):
        @functools.wraps(func)
        def func_with_requirement_test(self, *args, **kwargs):
            my_version = self._raw.version
            if my_version >= version:
                return func(self, *args, **kwargs)
            else:
                error_message = err_msg
                if error_message is None:
                    error_message = (
                        f"You called {func.__qualname__} which is not compatible with "
                        f"the {my_version.major}.{my_version.minor}.{my_version.patch}"
                        " version of Vasp you are using. Please use at least version "
                        f"{version.major}.{version.minor}.{version.patch} for this "
                        "feature."
                    )
                raise exception.OutdatedVaspVersion(error_message)

        return func_with_requirement_test

    return decorator_require


class Reader:
    "Helper class to deal with error handling of the array reading."

    def __init__(self, array):
        self._array = array
        self.shape = np.shape(array)

    def error_message(self, key, err):
        "We can overload this message in a subclass to make it more specific"
        return (
            "Error reading from the array, please check that the shape of the "
            "array is consistent with the access key."
        )

    def __getitem__(self, key):
        try:
            return self._array[key]
        except (ValueError, IndexError, TypeError) as err:
            raise exception.IncorrectUsage(self.error_message(key, err)) from err

    def __len__(self):
        return len(self._array)

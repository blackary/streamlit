# Copyright (c) Streamlit Inc. (2018-2022) Snowflake Inc. (2022-2024)
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

"""A bunch of useful utilities for dealing with dataframes."""

from __future__ import annotations

import contextlib
import copy
import math
from enum import Enum, EnumMeta, auto
from typing import (
    TYPE_CHECKING,
    Any,
    Final,
    Iterable,
    Protocol,
    Sequence,
    Tuple,
    TypeVar,
    Union,
    cast,
    overload,
)

from typing_extensions import TypeAlias, TypeGuard

import streamlit as st
from streamlit import config, errors, logger, string_util
from streamlit.type_util import is_iterable, is_type

if TYPE_CHECKING:
    import numpy as np
    import pyarrow as pa
    from pandas import DataFrame, Index, Series
    from pandas.core.indexing import _iLocIndexer
    from pandas.io.formats.style import Styler

_LOGGER: Final = logger.get_logger(__name__)


# Maximum number of rows to request from an unevaluated (out-of-core) dataframe
MAX_UNEVALUATED_DF_ROWS = 10000

_PANDAS_DF_TYPE_STR: Final = "pandas.core.frame.DataFrame"
_PANDAS_INDEX_TYPE_STR: Final = "pandas.core.indexes.base.Index"
_PANDAS_SERIES_TYPE_STR: Final = "pandas.core.series.Series"
_PANDAS_STYLER_TYPE_STR: Final = "pandas.io.formats.style.Styler"
_NUMPY_ARRAY_TYPE_STR: Final = "numpy.ndarray"
_SNOWPARK_DF_TYPE_STR: Final = "snowflake.snowpark.dataframe.DataFrame"
_SNOWPARK_DF_ROW_TYPE_STR: Final = "snowflake.snowpark.row.Row"
_SNOWPARK_TABLE_TYPE_STR: Final = "snowflake.snowpark.table.Table"
_PYSPARK_DF_TYPE_STR: Final = "pyspark.sql.dataframe.DataFrame"
_MODIN_DF_TYPE_STR: Final = "modin.pandas.dataframe.DataFrame"
_MODIN_SERIES_TYPE_STR: Final = "modin.pandas.series.Series"
_SNOWPANDAS_DF_TYPE_STR: Final = "snowflake.snowpark.modin.pandas.dataframe.DataFrame"
_SNOWPANDAS_SERIES_TYPE_STR: Final = "snowflake.snowpark.modin.pandas.series.Series"


_DATAFRAME_LIKE_TYPES: Final[tuple[str, ...]] = (
    _PANDAS_DF_TYPE_STR,
    _PANDAS_INDEX_TYPE_STR,
    _PANDAS_SERIES_TYPE_STR,
    _PANDAS_STYLER_TYPE_STR,
    _NUMPY_ARRAY_TYPE_STR,
)

# We show a special "UnevaluatedDataFrame" warning for cached funcs
# that attempt to return one of these unserializable types:
UNEVALUATED_DATAFRAME_TYPES = (
    _MODIN_DF_TYPE_STR,
    _MODIN_SERIES_TYPE_STR,
    _PYSPARK_DF_TYPE_STR,
    _SNOWPANDAS_DF_TYPE_STR,
    _SNOWPANDAS_SERIES_TYPE_STR,
    _SNOWPARK_DF_TYPE_STR,
    _SNOWPARK_TABLE_TYPE_STR,
)

DataFrameLike: TypeAlias = "Union[DataFrame, Index, Series, Styler]"

_DATAFRAME_COMPATIBLE_TYPES: Final[tuple[type, ...]] = (
    dict,
    list,
    set,
    tuple,
    type(None),
)

_DataFrameCompatible: TypeAlias = Union[dict, list, set, Tuple[Any], None]
DataFrameCompatible: TypeAlias = Union[_DataFrameCompatible, DataFrameLike]

V_co = TypeVar(
    "V_co",
    covariant=True,  # https://peps.python.org/pep-0484/#covariance-and-contravariance
)


class DataFrameGenericAlias(Protocol[V_co]):
    """Technically not a GenericAlias, but serves the same purpose in
    OptionSequence below, in that it is a type which admits DataFrame,
    but is generic. This allows OptionSequence to be a fully generic type,
    significantly increasing its usefulness.

    We can't use types.GenericAlias, as it is only available from python>=3.9,
    and isn't easily back-ported.
    """

    @property
    def iloc(self) -> _iLocIndexer: ...


OptionSequence: TypeAlias = Union[
    Iterable[V_co],
    DataFrameGenericAlias[V_co],
]


class DataFormat(Enum):
    """DataFormat is used to determine the format of the data."""

    UNKNOWN = auto()
    EMPTY = auto()  # None
    PANDAS_DATAFRAME = auto()  # pd.DataFrame
    PANDAS_SERIES = auto()  # pd.Series
    PANDAS_INDEX = auto()  # pd.Index
    NUMPY_LIST = auto()  # np.array[Scalar]
    NUMPY_MATRIX = auto()  # np.array[List[Scalar]]
    PYARROW_TABLE = auto()  # pyarrow.Table
    SNOWPARK_OBJECT = auto()  # Snowpark DataFrame, Table, List[Row]
    PYSPARK_OBJECT = auto()  # pyspark.DataFrame
    MODIN_OBJECT = auto()  # Modin DataFrame, Series
    SNOWPANDAS_OBJECT = auto()  # Snowpandas DataFrame, Series
    PANDAS_STYLER = auto()  # pandas Styler
    LIST_OF_RECORDS = auto()  # List[Dict[str, Scalar]]
    LIST_OF_ROWS = auto()  # List[List[Scalar]]
    LIST_OF_VALUES = auto()  # List[Scalar]
    TUPLE_OF_VALUES = auto()  # Tuple[Scalar]
    SET_OF_VALUES = auto()  # Set[Scalar]
    COLUMN_INDEX_MAPPING = auto()  # {column: {index: value}}
    COLUMN_VALUE_MAPPING = auto()  # {column: List[values]}
    COLUMN_SERIES_MAPPING = auto()  # {column: Series(values)}
    KEY_VALUE_DICT = auto()  # {index: value}


def is_dataframe(obj: object) -> TypeGuard[DataFrame]:
    return is_type(obj, _PANDAS_DF_TYPE_STR)


def is_dataframe_like(obj: object) -> TypeGuard[DataFrameLike]:
    return any(is_type(obj, t) for t in _DATAFRAME_LIKE_TYPES)


def is_unevaluated_data_object(obj: object) -> bool:
    """True if the object is one of the supported unevaluated data objects:

    Currently supported objects are:
    - Snowpark DataFrame / Table
    - PySpark DataFrame
    - Modin DataFrame / Series
    - Snowpandas DataFrame / Series

    Unevaluated means that the data is not yet in the local memory.
    Unevaluated data objects are treated differently from other data objects by only
    requesting a subset of the data instead of loading all data into th memory
    """
    return (
        is_snowpark_data_object(obj)
        or is_pyspark_data_object(obj)
        or is_snowpandas_data_object(obj)
        or is_modin_data_object(obj)
    )


def is_snowpark_data_object(obj: object) -> bool:
    """True if obj is a Snowpark DataFrame or Table."""
    return is_type(obj, _SNOWPARK_TABLE_TYPE_STR) or is_type(obj, _SNOWPARK_DF_TYPE_STR)


def is_snowpark_row_list(obj: object) -> bool:
    """True if obj is a list of snowflake.snowpark.row.Row."""
    if not isinstance(obj, list):
        return False
    if len(obj) < 1:
        return False
    if not hasattr(obj[0], "__class__"):
        return False
    return is_type(obj[0], _SNOWPARK_DF_ROW_TYPE_STR)


def is_pyspark_data_object(obj: object) -> bool:
    """True if obj is of type pyspark.sql.dataframe.DataFrame"""
    return (
        is_type(obj, _PYSPARK_DF_TYPE_STR)
        and hasattr(obj, "toPandas")
        and callable(obj.toPandas)
    )


def is_modin_data_object(obj: object) -> bool:
    """True if obj is of Modin Dataframe or Series"""
    return is_type(obj, _MODIN_DF_TYPE_STR) or is_type(obj, _MODIN_SERIES_TYPE_STR)


def is_snowpandas_data_object(obj: object) -> bool:
    """True if obj is a Snowpark Pandas DataFrame or Series."""
    return is_type(obj, _SNOWPANDAS_DF_TYPE_STR) or is_type(
        obj, _SNOWPANDAS_SERIES_TYPE_STR
    )


def is_dataframe_compatible(obj: object) -> TypeGuard[DataFrameCompatible]:
    """True if type that can be passed to convert_anything_to_df."""
    return is_dataframe_like(obj) or type(obj) in _DATAFRAME_COMPATIBLE_TYPES


def is_pandas_styler(obj: object) -> TypeGuard[Styler]:
    return is_type(obj, _PANDAS_STYLER_TYPE_STR)


def convert_anything_to_pandas_df(
    data: Any,
    max_unevaluated_rows: int = MAX_UNEVALUATED_DF_ROWS,
    ensure_copy: bool = False,
) -> DataFrame:
    """Try to convert different formats to a Pandas Dataframe.

    Parameters
    ----------
    data : any
        The data to convert to a Pandas DataFrame.

    max_unevaluated_rows: int
        If unevaluated data is detected this func will evaluate it,
        taking max_unevaluated_rows, defaults to 10k and 100 for st.table

    ensure_copy: bool
        If True, make sure to always return a copy of the data. If False, it depends on
        the type of the data. For example, a Pandas DataFrame will be returned as-is.

    Returns
    -------
    pandas.DataFrame

    """
    import pandas as pd

    if is_type(data, _PANDAS_DF_TYPE_STR):
        return data.copy() if ensure_copy else cast(pd.DataFrame, data)

    if is_pandas_styler(data):
        return cast("DataFrame", data.data.copy() if ensure_copy else data.data)

    if is_type(data, "numpy.ndarray"):
        return pd.DataFrame([]) if len(data.shape) == 0 else pd.DataFrame(data)
    if is_modin_data_object(data):
        data = data.head(max_unevaluated_rows)._to_pandas()

        if isinstance(data, pd.Series):
            data = data.to_frame()

        if data.shape[0] == max_unevaluated_rows:
            st.caption(
                f"⚠️ Showing only {string_util.simplify_number(max_unevaluated_rows)} "
                "rows. Call `_to_pandas()` on the dataframe to show more."
            )
        return cast(pd.DataFrame, data)

    if is_pyspark_data_object(data):
        data = data.limit(max_unevaluated_rows).toPandas()
        if data.shape[0] == max_unevaluated_rows:
            st.caption(
                f"⚠️ Showing only {string_util.simplify_number(max_unevaluated_rows)} "
                "rows. Call `toPandas()` on the dataframe to show more."
            )
        return cast(pd.DataFrame, data)

    if is_snowpark_data_object(data):
        data = data.limit(max_unevaluated_rows).to_pandas()
        if data.shape[0] == max_unevaluated_rows:
            st.caption(
                f"⚠️ Showing only {string_util.simplify_number(max_unevaluated_rows)} "
                "rows. Call `to_pandas()` on the dataframe to show more."
            )
        return cast(pd.DataFrame, data)

    if is_snowpandas_data_object(data):
        data = data.head(max_unevaluated_rows).to_pandas()

        if isinstance(data, pd.Series):
            data = data.to_frame()

        if data.shape[0] == max_unevaluated_rows:
            st.caption(
                f"⚠️ Showing only {string_util.simplify_number(max_unevaluated_rows)} "
                "rows. Call `to_pandas()` on the dataframe to show more."
            )
        return cast(pd.DataFrame, data)

    # This is inefficient when data is a pyarrow.Table as it will be converted
    # back to Arrow when marshalled to protobuf, but area/bar/line charts need
    # DataFrame magic to generate the correct output.
    if hasattr(data, "to_pandas"):
        return cast(pd.DataFrame, data.to_pandas())

    # Try to convert to pandas.DataFrame. This will raise an error is df is not
    # compatible with the pandas.DataFrame constructor.
    try:
        return pd.DataFrame(data)

    except ValueError as ex:
        if isinstance(data, dict):
            with contextlib.suppress(ValueError):
                # Try to use index orient as back-up to support key-value dicts
                return pd.DataFrame.from_dict(data, orient="index")
        raise errors.StreamlitAPIException(
            f"""
Unable to convert object of type `{type(data)}` to `pandas.DataFrame`.
Offending object:
```py
{data}
```"""
        ) from ex


@overload
def ensure_iterable(obj: Iterable[V_co]) -> Iterable[V_co]: ...


@overload
def ensure_iterable(obj: OptionSequence[V_co]) -> Iterable[Any]: ...


def ensure_iterable(obj: OptionSequence[V_co] | Iterable[V_co]) -> Iterable[Any]:
    """Try to convert different formats to something iterable. Most inputs
    are assumed to be iterable, but if we have a DataFrame, we can just
    select the first column to iterate over. If the input is not iterable,
    a TypeError is raised.

    Parameters
    ----------
    obj : list, tuple, numpy.ndarray, pandas.Series, pandas.DataFrame, pyspark.sql.DataFrame, snowflake.snowpark.dataframe.DataFrame or snowflake.snowpark.table.Table

    Returns
    -------
    iterable

    """

    if is_unevaluated_data_object(obj):
        obj = convert_anything_to_pandas_df(obj)

    if is_dataframe(obj):
        # Return first column as a pd.Series
        # The type of the elements in this column is not known up front, hence
        # the Iterable[Any] return type.
        return cast(Iterable[Any], obj.iloc[:, 0])

    if is_iterable(obj):
        return obj

    raise TypeError(
        f"Object is not an iterable and could not be converted to one. Object: {obj}"
    )


def ensure_indexable(obj: OptionSequence[V_co]) -> Sequence[V_co]:
    """Try to ensure a value is an indexable Sequence. If the collection already
    is one, it has the index method that we need. Otherwise, convert it to a list.
    """
    it = ensure_iterable(obj)
    # This is an imperfect check because there is no guarantee that an `index`
    # function actually does the thing we want.
    index_fn = getattr(it, "index", None)
    if callable(index_fn) and not isinstance(it, EnumMeta):
        # We return a shallow copy of the Sequence here because the return value of
        # this function is saved in a widget serde class instance to be used in later
        # script runs, and we don't want mutations to the options object passed to a
        # widget affect the widget.
        # (See https://github.com/streamlit/streamlit/issues/7534)
        return copy.copy(cast(Sequence[V_co], it))
    else:
        return list(it)


def _maybe_truncate_table(
    table: pa.Table, truncated_rows: int | None = None
) -> pa.Table:
    """Experimental feature to automatically truncate tables that
    are larger than the maximum allowed message size. It needs to be enabled
    via the server.enableArrowTruncation config option.

    Parameters
    ----------
    table : pyarrow.Table
        A table to truncate.

    truncated_rows : int or None
        The number of rows that have been truncated so far. This is used by
        the recursion logic to keep track of the total number of truncated
        rows.

    """

    if config.get_option("server.enableArrowTruncation"):
        # This is an optimization problem: We don't know at what row
        # the perfect cut-off is to comply with the max size. But we want to figure
        # it out in as few iterations as possible. We almost always will cut out
        # more than required to keep the iterations low.

        # The maximum size allowed for protobuf messages in bytes:
        max_message_size = int(config.get_option("server.maxMessageSize") * 1e6)
        # We add 1 MB for other overhead related to the protobuf message.
        # This is a very conservative estimate, but it should be good enough.
        table_size = int(table.nbytes + 1 * 1e6)
        table_rows = table.num_rows

        if table_rows > 1 and table_size > max_message_size:
            # targeted rows == the number of rows the table should be truncated to.
            # Calculate an approximation of how many rows we need to truncate to.
            targeted_rows = math.ceil(table_rows * (max_message_size / table_size))
            # Make sure to cut out at least a couple of rows to avoid running
            # this logic too often since it is quite inefficient and could lead
            # to infinity recursions without these precautions.
            targeted_rows = math.floor(
                max(
                    min(
                        # Cut out:
                        # an additional 5% of the estimated num rows to cut out:
                        targeted_rows - math.floor((table_rows - targeted_rows) * 0.05),
                        # at least 1% of table size:
                        table_rows - (table_rows * 0.01),
                        # at least 5 rows:
                        table_rows - 5,
                    ),
                    1,  # but it should always have at least 1 row
                )
            )
            sliced_table = table.slice(0, targeted_rows)
            return _maybe_truncate_table(
                sliced_table, (truncated_rows or 0) + (table_rows - targeted_rows)
            )

        if truncated_rows:
            displayed_rows = string_util.simplify_number(table.num_rows)
            total_rows = string_util.simplify_number(table.num_rows + truncated_rows)

            if displayed_rows == total_rows:
                # If the simplified numbers are the same,
                # we just display the exact numbers.
                displayed_rows = str(table.num_rows)
                total_rows = str(table.num_rows + truncated_rows)

            st.caption(
                f"⚠️ Showing {displayed_rows} out of {total_rows} "
                "rows due to data size limitations."
            )

    return table


def pyarrow_table_to_bytes(table: pa.Table) -> bytes:
    """Serialize pyarrow.Table to bytes using Apache Arrow.

    Parameters
    ----------
    table : pyarrow.Table
        A table to convert.

    """
    try:
        table = _maybe_truncate_table(table)
    except RecursionError as err:
        # This is a very unlikely edge case, but we want to make sure that
        # it doesn't lead to unexpected behavior.
        # If there is a recursion error, we just return the table as-is
        # which will lead to the normal message limit exceed error.
        _LOGGER.warning(
            "Recursion error while truncating Arrow table. This is not "
            "supposed to happen.",
            exc_info=err,
        )

    import pyarrow as pa

    # Convert table to bytes
    sink = pa.BufferOutputStream()
    writer = pa.RecordBatchStreamWriter(sink, table.schema)
    writer.write_table(table)
    writer.close()
    return cast(bytes, sink.getvalue().to_pybytes())


def is_colum_type_arrow_incompatible(column: Series[Any] | Index) -> bool:
    """Return True if the column type is known to cause issues during Arrow conversion."""
    from pandas.api.types import infer_dtype, is_dict_like, is_list_like

    if column.dtype.kind in [
        "c",  # complex64, complex128, complex256
    ]:
        return True

    if str(column.dtype) in {
        # These period types are not yet supported by our frontend impl.
        # See comments in Quiver.ts for more details.
        "period[B]",
        "period[N]",
        "period[ns]",
        "period[U]",
        "period[us]",
    }:
        return True

    if column.dtype == "object":
        # The dtype of mixed type columns is always object, the actual type of the column
        # values can be determined via the infer_dtype function:
        # https://pandas.pydata.org/docs/reference/api/pandas.api.types.infer_dtype.html
        inferred_type = infer_dtype(column, skipna=True)

        if inferred_type in [
            "mixed-integer",
            "complex",
        ]:
            return True
        elif inferred_type == "mixed":
            # This includes most of the more complex/custom types (objects, dicts, lists, ...)
            if len(column) == 0 or not hasattr(column, "iloc"):
                # The column seems to be invalid, so we assume it is incompatible.
                # But this would most likely never happen since empty columns
                # cannot be mixed.
                return True

            # Get the first value to check if it is a supported list-like type.
            first_value = column.iloc[0]

            if (
                not is_list_like(first_value)
                # dicts are list-like, but have issues in Arrow JS (see comments in Quiver.ts)
                or is_dict_like(first_value)
                # Frozensets are list-like, but are not compatible with pyarrow.
                or isinstance(first_value, frozenset)
            ):
                # This seems to be an incompatible list-like type
                return True
            return False
    # We did not detect an incompatible type, so we assume it is compatible:
    return False


def fix_arrow_incompatible_column_types(
    df: DataFrame, selected_columns: list[str] | None = None
) -> DataFrame:
    """Fix column types that are not supported by Arrow table.

    This includes mixed types (e.g. mix of integers and strings)
    as well as complex numbers (complex128 type). These types will cause
    errors during conversion of the dataframe to an Arrow table.
    It is fixed by converting all values of the column to strings
    This is sufficient for displaying the data on the frontend.

    Parameters
    ----------
    df : pandas.DataFrame
        A dataframe to fix.

    selected_columns: List[str] or None
        A list of columns to fix. If None, all columns are evaluated.

    Returns
    -------
    The fixed dataframe.
    """
    import pandas as pd

    # Make a copy, but only initialize if necessary to preserve memory.
    df_copy: DataFrame | None = None
    for col in selected_columns or df.columns:
        if is_colum_type_arrow_incompatible(df[col]):
            if df_copy is None:
                df_copy = df.copy()
            df_copy[col] = df[col].astype("string")

    # The index can also contain mixed types
    # causing Arrow issues during conversion.
    # Skipping multi-indices since they won't return
    # the correct value from infer_dtype
    if not selected_columns and (
        not isinstance(
            df.index,
            pd.MultiIndex,
        )
        and is_colum_type_arrow_incompatible(df.index)
    ):
        if df_copy is None:
            df_copy = df.copy()
        df_copy.index = df.index.astype("string")
    return df_copy if df_copy is not None else df


def data_frame_to_bytes(df: DataFrame) -> bytes:
    """Serialize pandas.DataFrame to bytes using Apache Arrow.

    Parameters
    ----------
    df : pandas.DataFrame
        A dataframe to convert.

    """
    import pyarrow as pa

    try:
        table = pa.Table.from_pandas(df)
    except (pa.ArrowTypeError, pa.ArrowInvalid, pa.ArrowNotImplementedError) as ex:
        _LOGGER.info(
            "Serialization of dataframe to Arrow table was unsuccessful due to: %s. "
            "Applying automatic fixes for column types to make the dataframe Arrow-compatible.",
            ex,
        )
        df = fix_arrow_incompatible_column_types(df)
        table = pa.Table.from_pandas(df)
    return pyarrow_table_to_bytes(table)


def bytes_to_data_frame(source: bytes) -> DataFrame:
    """Convert bytes to pandas.DataFrame.

    Using this function in production needs to make sure that
    the pyarrow version >= 14.0.1.

    Parameters
    ----------
    source : bytes
        A bytes object to convert.

    """
    import pyarrow as pa

    reader = pa.RecordBatchStreamReader(source)
    return reader.read_pandas()


def is_list_of_scalars(data: Iterable[Any]) -> bool:
    """Check if the list only contains scalar values."""
    from pandas.api.types import infer_dtype

    # Overview on all value that are interpreted as scalar:
    # https://pandas.pydata.org/docs/reference/api/pandas.api.types.is_scalar.html
    return infer_dtype(data, skipna=True) not in ["mixed", "unknown-array"]


def determine_data_format(input_data: Any) -> DataFormat:
    """Determine the data format of the input data.

    Parameters
    ----------
    input_data : Any
        The input data to determine the data format of.

    Returns
    -------
    DataFormat
        The data format of the input data.
    """
    import numpy as np
    import pandas as pd
    import pyarrow as pa

    if input_data is None:
        return DataFormat.EMPTY
    elif isinstance(input_data, pd.DataFrame):
        return DataFormat.PANDAS_DATAFRAME
    elif isinstance(input_data, np.ndarray):
        if len(input_data.shape) == 1:
            # For technical reasons, we need to distinguish one
            # one-dimensional numpy array from multidimensional ones.
            return DataFormat.NUMPY_LIST
        return DataFormat.NUMPY_MATRIX
    elif isinstance(input_data, pa.Table):
        return DataFormat.PYARROW_TABLE
    elif isinstance(input_data, pd.Series):
        return DataFormat.PANDAS_SERIES
    elif isinstance(input_data, pd.Index):
        return DataFormat.PANDAS_INDEX
    elif is_pandas_styler(input_data):
        return DataFormat.PANDAS_STYLER
    elif is_snowpark_data_object(input_data):
        return DataFormat.SNOWPARK_OBJECT
    elif is_modin_data_object(input_data):
        return DataFormat.MODIN_OBJECT
    elif is_snowpandas_data_object(input_data):
        return DataFormat.SNOWPANDAS_OBJECT
    elif is_pyspark_data_object(input_data):
        return DataFormat.PYSPARK_OBJECT
    elif isinstance(input_data, (list, tuple, set)):
        if is_list_of_scalars(input_data):
            # -> one-dimensional data structure
            if isinstance(input_data, tuple):
                return DataFormat.TUPLE_OF_VALUES
            if isinstance(input_data, set):
                return DataFormat.SET_OF_VALUES
            return DataFormat.LIST_OF_VALUES
        else:
            # -> Multi-dimensional data structure
            # This should always contain at least one element,
            # otherwise the values type from infer_dtype would have been empty
            first_element = next(iter(input_data))
            if isinstance(first_element, dict):
                return DataFormat.LIST_OF_RECORDS
            if isinstance(first_element, (list, tuple, set)):
                return DataFormat.LIST_OF_ROWS
    elif isinstance(input_data, dict):
        if not input_data:
            return DataFormat.KEY_VALUE_DICT
        if len(input_data) > 0:
            first_value = next(iter(input_data.values()))
            if isinstance(first_value, dict):
                return DataFormat.COLUMN_INDEX_MAPPING
            if isinstance(first_value, (list, tuple)):
                return DataFormat.COLUMN_VALUE_MAPPING
            if isinstance(first_value, pd.Series):
                return DataFormat.COLUMN_SERIES_MAPPING
            # In the future, we could potentially also support the tight & split formats here
            if is_list_of_scalars(input_data.values()):
                # Only use the key-value dict format if the values are only scalar values
                return DataFormat.KEY_VALUE_DICT
    return DataFormat.UNKNOWN


def _unify_missing_values(df: DataFrame) -> DataFrame:
    """Unify all missing values in a DataFrame to None.

    Pandas uses a variety of values to represent missing values, including np.nan,
    NaT, None, and pd.NA. This function replaces all of these values with None,
    which is the only missing value type that is supported by all data
    """
    import numpy as np

    return df.fillna(np.nan).replace([np.nan], [None])


def convert_df_to_data_format(
    df: DataFrame, data_format: DataFormat
) -> (
    DataFrame
    | Series[Any]
    | pa.Table
    | np.ndarray[Any, np.dtype[Any]]
    | tuple[Any]
    | list[Any]
    | set[Any]
    | dict[str, Any]
):
    """Convert a dataframe to the specified data format.

    Parameters
    ----------
    df : pd.DataFrame
        The dataframe to convert.

    data_format : DataFormat
        The data format to convert to.

    Returns
    -------
    pd.DataFrame, pd.Series, pyarrow.Table, np.ndarray, list, set, tuple, or dict.
        The converted dataframe.
    """

    if data_format in [
        DataFormat.EMPTY,
        DataFormat.PANDAS_DATAFRAME,
        DataFormat.SNOWPARK_OBJECT,
        DataFormat.PYSPARK_OBJECT,
        DataFormat.PANDAS_INDEX,
        DataFormat.PANDAS_STYLER,
        DataFormat.MODIN_OBJECT,
        DataFormat.SNOWPANDAS_OBJECT,
    ]:
        return df
    elif data_format == DataFormat.NUMPY_LIST:
        import numpy as np

        # It's a 1-dimensional array, so we only return
        # the first column as numpy array
        # Calling to_numpy() on the full DataFrame would result in:
        # [[1], [2]] instead of [1, 2]
        return np.ndarray(0) if df.empty else df.iloc[:, 0].to_numpy()
    elif data_format == DataFormat.NUMPY_MATRIX:
        import numpy as np

        return np.ndarray(0) if df.empty else df.to_numpy()
    elif data_format == DataFormat.PYARROW_TABLE:
        import pyarrow as pa

        return pa.Table.from_pandas(df)
    elif data_format == DataFormat.PANDAS_SERIES:
        # Select first column in dataframe and create a new series based on the values
        if len(df.columns) != 1:
            raise ValueError(
                f"DataFrame is expected to have a single column but has {len(df.columns)}."
            )
        return df[df.columns[0]]
    elif data_format == DataFormat.LIST_OF_RECORDS:
        return _unify_missing_values(df).to_dict(orient="records")
    elif data_format == DataFormat.LIST_OF_ROWS:
        # to_numpy converts the dataframe to a list of rows
        return _unify_missing_values(df).to_numpy().tolist()
    elif data_format == DataFormat.COLUMN_INDEX_MAPPING:
        return _unify_missing_values(df).to_dict(orient="dict")
    elif data_format == DataFormat.COLUMN_VALUE_MAPPING:
        return _unify_missing_values(df).to_dict(orient="list")
    elif data_format == DataFormat.COLUMN_SERIES_MAPPING:
        return df.to_dict(orient="series")
    elif data_format in [
        DataFormat.LIST_OF_VALUES,
        DataFormat.TUPLE_OF_VALUES,
        DataFormat.SET_OF_VALUES,
    ]:
        df = _unify_missing_values(df)
        return_list = []
        if len(df.columns) == 1:
            #  Get the first column and convert to list
            return_list = df[df.columns[0]].tolist()
        elif len(df.columns) >= 1:
            raise ValueError(
                f"DataFrame is expected to have a single column but has {len(df.columns)}."
            )
        if data_format == DataFormat.TUPLE_OF_VALUES:
            return tuple(return_list)
        if data_format == DataFormat.SET_OF_VALUES:
            return set(return_list)
        return return_list
    elif data_format == DataFormat.KEY_VALUE_DICT:
        df = _unify_missing_values(df)
        # The key is expected to be the index -> this will return the first column
        # as a dict with index as key.
        return {} if df.empty else df.iloc[:, 0].to_dict()

    raise ValueError(f"Unsupported input data format: {data_format}")
from copy import deepcopy
from collections.abc import Mapping as C_Mapping
from typing import List, cast, Any

from dlt.common.schema.utils import merge_columns, new_column, new_table
from dlt.common.schema.typing import TColumnProp, TColumnSchema, TPartialTableSchema, TTableSchemaColumns, TWriteDisposition
from dlt.common.typing import TDataItem

from dlt.extract.typing import TColumnKey, TFunHintTemplate, TTableHintTemplate, TTableSchemaTemplate
from dlt.extract.exceptions import DataItemRequiredForDynamicTableHints, InconsistentTableTemplate, TableNameMissing


class DltResourceSchema:
    def __init__(self, name: str, table_schema_template: TTableSchemaTemplate = None):
        self.__qualname__ = self.__name__ = self.name = name
        self._table_name_hint_fun: TFunHintTemplate[str] = None
        self._table_has_other_dynamic_hints: bool = False
        self._table_schema_template: TTableSchemaTemplate = None
        self._table_schema: TPartialTableSchema = None
        if table_schema_template:
            self.set_template(table_schema_template)

    @property
    def table_name(self) -> str:
        """Get table name to which resource loads data. Raises in case of table names derived from data."""
        if self._table_name_hint_fun:
            raise DataItemRequiredForDynamicTableHints(self.name)
        return self._table_schema_template["name"] if self._table_schema_template else self.name  # type: ignore

    def table_schema(self, item: TDataItem =  None) -> TPartialTableSchema:
        """Computes the table schema based on hints and column definitions passed during resource creation. `item` parameter is used to resolve table hints based on data"""
        if not self._table_schema_template:
            # if table template is not present, generate partial table from name
            if not self._table_schema:
                self._table_schema = new_table(self.name)
            return self._table_schema

        def _resolve_hint(hint: TTableHintTemplate[Any]) -> Any:
            """Calls each dynamic hint passing a data item"""
            if callable(hint):
                return hint(item)
            else:
                return hint

        def _merge_key(hint: TColumnProp, keys: TColumnKey, partial: TPartialTableSchema) -> None:
            if isinstance(keys, str):
                keys = [keys]
            for key in keys:
                if key in partial["columns"]:
                    merge_columns(partial["columns"][key], {hint: key})  # type: ignore
                else:
                    partial["columns"][key] = new_column(key, nullable=False)
                    partial["columns"][key][hint] = True

        def _merge_keys(t_: TTableSchemaTemplate) -> TPartialTableSchema:
            """Merges resolved keys into columns"""
            partial = cast(TPartialTableSchema, t_)
            # assert not callable(t_["merge_key"])
            # assert not callable(t_["primary_key"])
            if "primary_key" in t_:
                _merge_key("primary_key", t_.pop("primary_key"), partial)  # type: ignore
            if "merge_key" in t_:
                _merge_key("merge_key", t_.pop("merge_key"), partial)  # type: ignore

            return partial

        # if table template present and has dynamic hints, the data item must be provided
        if self._table_name_hint_fun:
            if item is None:
                raise DataItemRequiredForDynamicTableHints(self.name)
            else:
                resolved_template: TTableSchemaTemplate = {k: _resolve_hint(v) for k, v in self._table_schema_template.items()}  # type: ignore
                return _merge_keys(resolved_template)
        else:
            return _merge_keys(self._table_schema_template)

    def apply_hints(
        self,
        table_name: TTableHintTemplate[str] = None,
        parent_table_name: TTableHintTemplate[str] = None,
        write_disposition: TTableHintTemplate[TWriteDisposition] = None,
        columns: TTableHintTemplate[TTableSchemaColumns] = None,
        primary_key: TTableHintTemplate[TColumnKey] = None,
        merge_key: TTableHintTemplate[TColumnKey] = None
    ) -> None:
        """Allows to create or modify existing table schema by setting provided hints. Accepts dynamic hints based on data.
           Pass None to keep old value
           Pass empty value (for particular type) to remove hint
        """
        t = None
        if not self._table_schema_template:
            # if there's no template yet, create and set new one
            t = self.new_table_template(table_name, parent_table_name, write_disposition, columns, primary_key, merge_key)
        else:
            # set single hints
            t = deepcopy(self._table_schema_template)
            if table_name is not None:
                if table_name:
                    t["name"] = table_name
                else:
                    t.pop("name", None)
            if parent_table_name is not None:
                if parent_table_name:
                    t["parent"] = parent_table_name
                else:
                    t.pop("parent", None)
            if write_disposition:
                t["write_disposition"] = write_disposition
            if columns is not None:
                t["columns"] = columns
            if primary_key is not None:
                t["primary_key"] = primary_key
            if merge_key is not None:
                t["merge_key"] = merge_key
        self.set_template(t)

    def set_template(self, table_schema_template: TTableSchemaTemplate) -> None:
        # if "name" is callable in the template then the table schema requires actual data item to be inferred
        name_hint = table_schema_template["name"]
        if callable(name_hint):
            self._table_name_hint_fun = name_hint
        else:
            self._table_name_hint_fun = None
        # check if any other hints in the table template should be inferred from data
        self._table_has_other_dynamic_hints = any(callable(v) for k, v in table_schema_template.items() if k != "name")
        self._table_schema_template = table_schema_template

    @staticmethod
    def new_table_template(
        table_name: TTableHintTemplate[str],
        parent_table_name: TTableHintTemplate[str] = None,
        write_disposition: TTableHintTemplate[TWriteDisposition] = None,
        columns: TTableHintTemplate[TTableSchemaColumns] = None,
        primary_key: TTableHintTemplate[TColumnKey] = None,
        merge_key: TTableHintTemplate[TColumnKey] = None
        ) -> TTableSchemaTemplate:
        if not table_name:
            raise TableNameMissing()

        # create a table schema template where hints can be functions taking TDataItem
        if isinstance(columns, C_Mapping):
            # new_table accepts a sequence
            column_list: List[TColumnSchema] = []
            for name, column in columns.items():
                column["name"] = name
                column_list.append(column)
            columns = column_list  # type: ignore

        new_template: TTableSchemaTemplate = new_table(table_name, parent_table_name, write_disposition=write_disposition, columns=columns)  # type: ignore
        if primary_key:
            new_template["primary_key"] = primary_key
        if merge_key:
            new_template["merge_key"] = merge_key
        # if any of the hints is a function then name must be as well
        if any(callable(v) for k, v in new_template.items() if k != "name") and not callable(table_name):
            raise InconsistentTableTemplate(f"Table name {table_name} must be a function if any other table hint is a function")
        return new_template

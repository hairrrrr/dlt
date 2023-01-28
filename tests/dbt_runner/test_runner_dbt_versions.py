import os
import shutil
import tempfile
from typing import Any, Iterator, List
from functools import partial
import pytest
from dlt.common import json

from dlt.common.configuration import resolve_configuration
from dlt.common.storages.file_storage import FileStorage
from dlt.common.runners.synth_pickle import decode_obj, encode_obj
from dlt.common.runners.venv import Venv
from dlt.common.typing import AnyFun
from dlt.helpers.dbt.configuration import DBTRunnerConfiguration

from dlt.destinations.postgres.postgres import PostgresClient
from dlt.helpers.dbt.exceptions import PrerequisitesException, DBTProcessingError

from dlt.helpers.dbt import package_runner
from tests.dbt_runner.utils import JAFFLE_SHOP_REPO, assert_jaffle_completed, clone_jaffle_repo, find_run_result

from tests.utils import test_storage, preserve_environ
from tests.load.utils import yield_client_with_storage


@pytest.fixture(scope="function")
def client() -> Iterator[PostgresClient]:
    yield from yield_client_with_storage("postgres")


@pytest.fixture(
    scope="module",
    params=[
        ["dbt-core==1.1.3", "dbt-postgres==1.1.3"],
        ["dbt-core==1.2.4", "dbt-postgres==1.2.4"],
        ["dbt-core==1.3.2", "dbt-postgres==1.3.2"],
        None
        ],
    ids=["venv-1.1.3", "venv-1.2.4", "venv-1.3.2", "local"]
)
def dbt_package_f(request: Any) -> AnyFun:
    if request.param is None:
        yield partial(package_runner, Venv.restore_current())
    else:
        with Venv.create(tempfile.mkdtemp(), [os.getcwd()] + request.param) as venv:
            yield partial(package_runner, venv)


def test_dbt_configuration() -> None:
    # check names normalized
    C: DBTRunnerConfiguration = resolve_configuration(
        DBTRunnerConfiguration(),
        explicit_value={"package_repository_ssh_key": "---NO NEWLINE---", "package_location": "/var/local"}
    )
    assert C.package_repository_ssh_key == "---NO NEWLINE---\n"
    assert C.package_additional_vars is None
    # profiles are set to the module dir
    assert C.package_profiles_dir.endswith("dbt_runner")

    C = resolve_configuration(
        DBTRunnerConfiguration(),
        explicit_value={"package_repository_ssh_key": "---WITH NEWLINE---\n", "package_location": "/var/local", "package_additional_vars": {"a": 1}}
    )
    assert C.package_repository_ssh_key == "---WITH NEWLINE---\n"
    assert C.package_additional_vars == {"a": 1}


def test_dbt_run_exception_pickle() -> None:
    obj = decode_obj(encode_obj(DBTProcessingError("test", "A", "B"), ignore_pickle_errors=False), ignore_pickle_errors=False)
    assert obj.command == "test"
    assert obj.run_results == "A"
    assert obj.dbt_results == "B"
    assert str(obj) == "DBT command test could not be executed"


def test_runner_setup(client: PostgresClient, test_storage: FileStorage) -> None:
    add_vars = {"source_dataset_name": "overwritten", "destination_dataset_name": "destination", "schema_name": "this_Schema"}
    os.environ["DBT_PACKAGE_RUNNER__PACKAGE_ADDITIONAL_VARS"] = json.dumps(add_vars)
    os.environ["AUTO_FULL_REFRESH_WHEN_OUT_OF_SYNC"] = "False"
    os.environ["DBT_PACKAGE_RUNNER__RUNTIME__LOG_LEVEL"] = "CRITICAL"
    test_storage.create_folder("jaffle")
    r = package_runner(Venv.restore_current(), client.config, test_storage.make_full_path("jaffle"), JAFFLE_SHOP_REPO)
    # runner settings
    assert r.credentials is client.config.credentials
    assert r.working_dir == test_storage.make_full_path("jaffle")
    assert r.source_dataset_name == client.config.dataset_name
    assert client.config.dataset_name.startswith("test")
    # runner config init
    assert r.config.package_location == JAFFLE_SHOP_REPO
    assert r.config.package_repository_branch is None
    assert r.config.package_repository_ssh_key == ""
    assert r.config.package_profile_name == "postgres"
    assert r.config.package_profiles_dir.endswith("dbt_runner")
    assert r.config.package_additional_vars == add_vars
    assert r.config.runtime.log_level == "CRITICAL"
    assert r.config.auto_full_refresh_when_out_of_sync is False

    assert r._get_package_vars() == {"source_dataset_name": client.config.dataset_name, "destination_dataset_name": "destination", "schema_name": "this_Schema"}
    assert r._get_package_vars(destination_dataset_name="dest_test_123") == {"source_dataset_name": client.config.dataset_name, "destination_dataset_name": "dest_test_123", "schema_name": "this_Schema"}
    assert r._get_package_vars(additional_vars={"add": 1, "schema_name": "ovr"}) == {
            "source_dataset_name": client.config.dataset_name,
            "destination_dataset_name": "destination", "schema_name": "ovr",
            "add": 1
        }


def test_run_jaffle_from_repo(client: PostgresClient, test_storage: FileStorage, dbt_package_f: AnyFun) -> None:
    test_storage.create_folder("jaffle")
    results = dbt_package_f(
            client.config,
            test_storage.make_full_path("jaffle"),
            JAFFLE_SHOP_REPO
        ).run_all(["--fail-fast", "--full-refresh"])
    assert_jaffle_completed(test_storage, results)


def test_run_jaffle_from_folder_incremental(client: PostgresClient, test_storage: FileStorage, dbt_package_f: AnyFun) -> None:
    repo_path = clone_jaffle_repo(test_storage)
    # copy model with error into package to force run error in model
    shutil.copy("./tests/dbt_runner/cases/jaffle_customers_incremental.sql", os.path.join(repo_path, "models", "customers.sql"))
    results = dbt_package_f(client.config, None, repo_path).run_all(run_params=None)
    assert_jaffle_completed(test_storage, results, jaffle_dir="jaffle_shop")
    results = dbt_package_f(client.config, None, repo_path).run_all()
    # out of 100 records 0 was inserted
    customers = find_run_result(results, "customers")
    assert customers.message == "INSERT 0 100"
    # change the column name. that will force dbt to fail (on_schema_change='fail'). the runner should do a full refresh
    shutil.copy("./tests/dbt_runner/cases/jaffle_customers_incremental_new_column.sql", os.path.join(repo_path, "models", "customers.sql"))
    results = dbt_package_f(client.config, None, repo_path).run_all(run_params=None)
    assert_jaffle_completed(test_storage, results, jaffle_dir="jaffle_shop")


def test_run_jaffle_fail_prerequisites(client: PostgresClient, test_storage: FileStorage, dbt_package_f: AnyFun) -> None:
    test_storage.create_folder("jaffle")
    # we run all the tests before tables are materialized
    with pytest.raises(PrerequisitesException) as pr_exc:
        dbt_package_f(
                client.config,
                test_storage.make_full_path("jaffle"),
                JAFFLE_SHOP_REPO
            ).run_all(["--fail-fast", "--full-refresh"], source_tests_selector="*")
    proc_err = pr_exc.value.args[0]
    assert isinstance(proc_err, DBTProcessingError)
    customers = find_run_result(proc_err.run_results, "unique_customers_customer_id")
    assert customers.status == "error"
    assert len(proc_err.run_results) == 20
    assert all(r.status == "error" for r in proc_err.run_results)


def test_run_jaffle_invalid_run_args(client: PostgresClient, test_storage: FileStorage, dbt_package_f: AnyFun) -> None:
    test_storage.create_folder("jaffle")
    # we run all the tests before tables are materialized
    with pytest.raises(DBTProcessingError) as pr_exc:
        dbt_package_f(client.config, test_storage.make_full_path("jaffle"), JAFFLE_SHOP_REPO).run_all(["--wrong_flag"])
    assert isinstance(pr_exc.value.dbt_results, SystemExit)


def test_run_jaffle_failed_run(client: PostgresClient, test_storage: FileStorage, dbt_package_f: AnyFun) -> None:
    repo_path = clone_jaffle_repo(test_storage)
    # copy model with error into package to force run error in model
    shutil.copy("./tests/dbt_runner/cases/jaffle_customers_with_error.sql", os.path.join(repo_path, "models", "customers.sql"))
    with pytest.raises(DBTProcessingError) as pr_exc:
        dbt_package_f(client.config, None, repo_path).run_all(run_params=None)
    assert len(pr_exc.value.run_results) == 5
    customers = find_run_result(pr_exc.value.run_results, "customers")
    assert customers.status == "error"

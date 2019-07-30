from collections import defaultdict
from copy import deepcopy
from git import Repo
from git.exc import GitCommandError
from json import loads
from json.decoder import JSONDecodeError
from logging import getLogger
from multiprocessing import Lock
from multiprocessing.pool import ThreadPool
from napalm import get_network_driver
from napalm.base.base import NetworkDriver
from netmiko import ConnectHandler
from pathlib import Path
from paramiko import SSHClient
from re import compile, search
from scp import SCPClient
from sqlalchemy import (
    Boolean,
    case,
    Column,
    ForeignKey,
    Integer,
    PickleType,
    String,
    Text,
)
from sqlalchemy.ext.associationproxy import association_proxy
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import backref, relationship
from time import sleep
from traceback import format_exc
from typing import Any, Dict, Generator, List, Match, Optional, Set, Tuple, Union
from xmltodict import parse
from xml.parsers.expat import ExpatError

from eNMS.concurrency import get_device_result
from eNMS.controller import controller
from eNMS.database import (
    CustomMediumBlobPickle,
    LARGE_STRING_LENGTH,
    Session,
    SMALL_STRING_LENGTH,
)
from eNMS.database.functions import convert_value, factory, fetch
from eNMS.database.associations import (
    job_device_table,
    job_event_table,
    job_pool_table,
    job_workflow_table,
)
from eNMS.database.base import AbstractBase
from eNMS.models.inventory import Device
from eNMS.models.events import Task  # noqa: F401


class Result(AbstractBase):

    __tablename__ = type = "Result"
    private = True
    id = Column(Integer, primary_key=True)
    success = Column(Boolean, default=False)
    result = Column(MutableDict.as_mutable(PickleType), default={})
    run_id = Column(Integer, ForeignKey("Run.id"))
    run = relationship("Run", back_populates="results", foreign_keys="Result.run_id")
    device_id = Column(Integer, ForeignKey("Device.id"))
    device = relationship(
        "Device", back_populates="results", foreign_keys="Result.device_id"
    )
    device_name = association_proxy("device", "name")

    def __init__(self, **kwargs):
        self.success = kwargs["result"]["success"]
        super().__init__(**kwargs)

    def __repr__(self):
        return f"{self.run} {self.device_name}"


class Run(AbstractBase):

    __tablename__ = type = "Run"
    private = True
    id = Column(Integer, primary_key=True)
    properties = Column(MutableDict.as_mutable(PickleType), default={})
    success = Column(Boolean, default=False)
    status = Column(String(SMALL_STRING_LENGTH), default="Running")
    runtime = Column(String(SMALL_STRING_LENGTH), default="")
    endtime = Column(String(SMALL_STRING_LENGTH), default="")
    parent_runtime = Column(String(SMALL_STRING_LENGTH), default="")
    job_id = Column(Integer, ForeignKey("Job.id"))
    job = relationship("Job", back_populates="runs", foreign_keys="Run.job_id")
    job_name = association_proxy("job", "name")
    workflow_id = Column(Integer, ForeignKey("Workflow.id"))
    workflow = relationship("Workflow", foreign_keys="Run.workflow_id")
    workflow_name = association_proxy("workflow", "name")
    task_id = Column(Integer, ForeignKey("Task.id"))
    task = relationship("Task", foreign_keys="Run.task_id")
    results = relationship("Result", back_populates="run")

    def __init__(self, **kwargs):
        self.runtime = kwargs.get("runtime") or controller.get_time()
        if not kwargs.get("parent_runtime"):
            self.parent_runtime = self.runtime
        super().__init__(**kwargs)

    def __repr__(self) -> str:
        return f"{self.runtime} ({self.job_name})"

    def __getitem__(self, key):
        if key in self.properties:
            return convert_value(self.job.type, key, self.properties[key], "id")
        elif hasattr(self.job, key):
            return getattr(self.job, key)
        else:
            raise AttributeError

    def generate_row(self, table: str) -> List[str]:
        return [
            f"""<button type="button" class="btn btn-info btn-xs"
            onclick="showLogs('{self.runtime}', '{self.job_name}')">
            </i>Logs</a></button>""",
            f"""<button type="button" class="btn btn-info btn-xs"
            onclick="showResultsPanel('{self.id}', '{self.name}', 'run')">
            </i>Results</a></button>""",
            f"""<button type="button" class="btn btn-primary btn-xs"
            onclick="showTypePanel('run', '{self.id}')">View</button>""",
        ]

    @property
    def progress(self) -> str:
        if self.status == "Running":
            progress = controller.job_db[self.runtime]
            try:
                return (
                    f"{progress['completed']}/{progress['number_of_targets']}"
                    f" ({progress['failed']} failed)"
                )
            except KeyError:
                return "N/A"
        else:
            return "N/A"

    def netmiko_connection(self, device: "Device") -> ConnectHandler:
        if self.parent_runtime in controller.connections_cache["netmiko"]:
            parent_connection = controller.connections_cache["netmiko"].get(
                self.parent_runtime
            )
            if parent_connection and device.name in parent_connection:
                if self.job.start_new_connection:
                    parent_connection.pop(device.name).disconnect()
                else:
                    try:
                        connection = parent_connection[device.name]
                        connection.find_prompt()
                        for property in ("fast_cli", "timeout", "global_delay_factor"):
                            setattr(connection, property, getattr(self.job, property))
                        return connection
                    except (OSError, ValueError):
                        parent_connection.pop(device.name)
        username, password = self.get_credentials(device)
        driver = device.netmiko_driver if self["use_device_driver"] else self["driver"]
        netmiko_connection = ConnectHandler(
            device_type=driver,
            ip=device.ip_address,
            port=getattr(device, "port"),
            username=username,
            password=password,
            secret=device.enable_password,
            fast_cli=self["fast_cli"],
            timeout=self["timeout"],
            global_delay_factor=self["global_delay_factor"],
        )
        if self["privileged_mode"]:
            netmiko_connection.enable()
        if self.workflow:
            controller.connections_cache["netmiko"][self.parent_runtime][
                device.name
            ] = netmiko_connection
        return netmiko_connection

    def napalm_connection(self, device: "Device") -> NetworkDriver:
        if self.parent_runtime in controller.connections_cache["napalm"]:
            parent_connection = controller.connections_cache["napalm"].get(
                self.parent_runtime
            )
            if parent_connection and device.name in parent_connection:
                if (
                    self.job.start_new_connection
                    or not parent_connection[device.name].is_alive()
                ):
                    parent_connection.pop(device.name).close()
                else:
                    return parent_connection[device.name]
        username, password = self.get_credentials(device)
        optional_args = self.job.optional_args
        if not optional_args:
            optional_args = {}
        if "secret" not in optional_args:
            optional_args["secret"] = device.enable_password
        driver = get_network_driver(
            device.napalm_driver if self["use_device_driver"] else self["driver"]
        )
        napalm_connection = driver(
            hostname=device.ip_address,
            username=username,
            password=password,
            optional_args=optional_args,
        )
        napalm_connection.open()
        if self.workflow:
            controller.connections_cache["napalm"][self.parent_runtime][
                device.name
            ] = napalm_connection
        return napalm_connection

    def payload_helper(
        self,
        payload: dict,
        name: str,
        value: Optional[Any] = None,
        service: Optional[str] = None,
        section: Optional[str] = None,
        device: Optional[str] = None,
    ) -> Any:
        if not service:
            payload = payload.setdefault("variables", {})
        else:
            payload = payload[service]
        if device:
            payload = payload.setdefault("devices", {})
            payload = payload.setdefault(device, {})
        if section:
            payload = payload.setdefault(section, {})
        if value:
            print(value)
            payload[name] = value
        else:
            if name not in payload:
                raise Exception(f"Payload Editor: {name} not found in {payload}.")
            return payload[name]

    def compute_devices(
        self, payload: Optional[dict] = None, device: Optional["Device"] = None
    ) -> Set["Device"]:

        if self.job.python_query:

            def get_var(*args: Any, **kwargs: Any) -> Any:
                return self.payload_helper(payload, *args, **kwargs)

            try:
                values = eval(self.job.python_query, locals())
            except Exception as exc:
                raise Exception(f"Python Query Failure: {str(exc)}")
            devices, not_found = set(), set()
            if isinstance(values, str):
                values = [values]
            for value in values:
                device = fetch(
                    "Device", allow_none=True, **{self.job.query_property_type: value}
                )
                if device:
                    devices.add(device)
                else:
                    not_found.add(value)
            if not_found:
                raise Exception(f"Python query invalid targets: {', '.join(not_found)}")
        else:
            devices = set(self["devices"])
            for pool in self["pools"]:
                devices |= set(pool.devices)
        controller.job_db[self.runtime]["number_of_targets"] = len(devices)
        Session.commit()
        return devices

    def close_connection_cache(self):
        for library in ("netmiko", "napalm"):
            connections = controller.connections_cache[library].pop(self.runtime, None)
            if not connections:
                continue
            for device, conn in connections.items():
                self.log("info", f"Closing {library} Connection to {device}")
                conn.disconnect() if library == "netmiko" else conn.close()

    def run(
        self, payload: Optional[dict] = None, start_points: Optional[List["Job"]] = None
    ) -> Tuple[dict, str]:
        self.job.status, self.job.state = "Running", {}
        self.log("info", f"{self.job.type} {self.job.name}: Starting")
        Session.commit()
        try:
            results = self.job.build_results(
                self, payload or self["initial_payload"], start_points
            )
            self.close_connection_cache()
            self.log("info", f"{self.job.type} {self.job.name}: Finished")
        except Exception as exc:
            result = (
                f"Running {self.job.type} '{self.job.name}' raised the following exception:\n"
                f"{chr(10).join(format_exc().splitlines())}\n\nRun aborted..."
            )
            self.log("error", result)
            results = {"success": False, "results": result}
        finally:
            self.job.status, self.job.state = "Idle", {}
            self.status = f"Completed ({'success' if self.success else 'failure'})"
            controller.job_db[self.runtime]["completed"] = 0
            controller.job_db[self.runtime]["failed"] = 0
            results["endtime"] = self.endtime = controller.get_time()
            results["logs"] = controller.run_logs.pop(self.runtime)
            if self.task and not self.task.frequency:
                self.task.is_active = False
            results["properties"] = {
                "run": self.properties,
                "service": self.job.to_dict(True),
            }
            self.create_result(results)
            Session.commit()
        if not self.workflow and self["send_notification"]:
            self.notify(results)
        return results, self.runtime

    def create_result(self, results: dict, device: Optional["Device"] = None):
        self.success = results["success"]
        result_kw = {"run": self, "result": results}
        if device:
            result_kw["device"] = device.id
        factory("Result", **result_kw)

    def get_results(self, payload: dict, device: Optional["Device"] = None) -> dict:
        self.log(
            "info", f"Running {self.job.type}{f' on {device.name}' if device else ''}"
        )
        results: Dict[Any, Any] = {"runtime": controller.get_time()}
        try:
            if device:
                results.update(self.job.job(self, payload, device))
            else:
                results.update(self.job.job(self, payload))
        except Exception:
            results.update(
                {"success": False, "result": chr(10).join(format_exc().splitlines())}
            )
        self.log(
            "info",
            f"Finished running {self.job.type} '{self.job.name}'"
            f"({'SUCCESS' if results['success'] else 'FAILURE'})"
            f"{f' on {device.name}' if device else ''}",
        )
        controller.job_db[self.runtime]["completed"] += 1
        controller.job_db[self.runtime]["failed"] += 1 - results["success"]
        if device:
            self.create_result(results, device)
        return results

    def log(self, severity: str, log: str) -> None:
        log = f"{controller.get_time()} - {severity} - {log}"
        controller.run_logs[self.runtime].append(log)
        if self.workflow:
            controller.run_logs[self.parent_runtime].append(log)

    def run_notification(self, results: dict) -> List[str]:
        notification = self["notification_header"].splitlines()
        if self.job.type == "Workflow":
            return notification
        elif "devices" in results["results"] and not results["success"]:
            failed = "\n".join(
                device
                for device, device_results in results["results"]["devices"].items()
                if not device_results["success"]
            )
            notification.append(f"FAILED :\n{failed}")
            if not self["display_only_failed_nodes"]:
                passed = "\n".join(
                    device
                    for device, device_results in results["results"]["devices"].items()
                    if device_results["success"]
                )
                notification.append(f"\n\nPASS :\n{passed}")
        return notification

    def notify(self, results: dict) -> None:
        notification = [
            f"Job: {self.job.name} ({self.job.type})",
            f"Runtime: {self.runtime}",
            f'Status: {"PASS" if results["success"] else "FAILED"}',
        ]
        notification.extend(self.run_notification(results))
        if self["include_link_in_summary"]:
            notification.append(
                f"Results: {controller.enms_server_addr}/view_job_results"
                f"/{self.id}/{self.runtime.replace(' ', '$')}"
            )
        notification_payload = {
            "job": self.job.get_properties(),
            "results": results,
            "content": "\n\n".join(notification),
        }
        notification_run = factory(
            "Run", **{"job": fetch("Job", name=self["send_notification_method"]).id}
        )
        notification_run.run(notification_payload)

    def get_credentials(self, device: "Device") -> Tuple[str, str]:
        return (
            controller.get_user_credentials()
            if self["credentials"] == "user"
            else (device.username, device.password)
            if self["credentials"] == "device"
            else (
                self.sub(self.job.custom_username, locals()),
                self.sub(self.job.custom_password, locals()),
            )
        )

    def convert_result(self, result: Any) -> Union[str, dict]:
        try:
            if self["conversion_method"] == "json":
                result = loads(result)
            elif self["conversion_method"] == "xml":
                result = parse(result)
        except (ExpatError, JSONDecodeError) as e:
            result = {
                "success": False,
                "text_response": result,
                "error": f"Conversion to {self['conversion_method']} failed",
                "exception": str(e),
            }
        return result

    def match_content(self, result: Any, match: Union[str, dict]) -> bool:
        if self["validation_method"] == "text":
            result = str(result)
            assert isinstance(match, str)
            if self["delete_spaces_before_matching"]:
                match, result = map(self["space_deleter"], (match, result))
            success = (
                self["content_match_regex"]
                and bool(search(match, result))
                or match in result
                and not self["content_match_regex"]
            )
        else:
            assert isinstance(match, dict)
            success = self.match_dictionary(result, match)
        return success if not self["negative_logic"] else not success

    def match_dictionary(self, result: dict, match: dict) -> bool:
        if self["validation_method"] == "dict_equal":
            return result == self.dict_match
        else:
            match_copy = deepcopy(match)
            for k, v in result.items():
                if isinstance(v, dict):
                    return self.match_dictionary(v, match_copy)
                elif k in match_copy and match_copy[k] == v:
                    match_copy.pop(k)
            return not match_copy

    def transfer_file(
        self, ssh_client: SSHClient, files: List[Tuple[str, str]]
    ) -> None:
        if self["protocol"] == "sftp":
            sftp = ssh_client.open_sftp()
            for source, destination in files:
                getattr(sftp, self["direction"])(source, destination)
            sftp.close()
        else:
            with SCPClient(ssh_client.get_transport()) as scp:
                for source, destination in files:
                    getattr(scp, self["direction"])(source, destination)

    @property
    def name(self) -> str:
        return repr(self)


class Job(AbstractBase):

    __tablename__ = "Job"
    type = Column(String(SMALL_STRING_LENGTH), default="")
    __mapper_args__ = {"polymorphic_identity": "Job", "polymorphic_on": type}
    id = Column(Integer, primary_key=True)
    hidden = Column(Boolean, default=False)
    name = Column(String(SMALL_STRING_LENGTH), unique=True)
    last_modified = Column(String(SMALL_STRING_LENGTH), default="")
    description = Column(String(SMALL_STRING_LENGTH), default="")
    number_of_retries = Column(Integer, default=0)
    time_between_retries = Column(Integer, default=10)
    positions = Column(MutableDict.as_mutable(PickleType), default={})
    status = Column(String(SMALL_STRING_LENGTH), default="Idle")
    state = Column(MutableDict.as_mutable(PickleType), default={})
    credentials = Column(String(SMALL_STRING_LENGTH), default="device")
    tasks = relationship("Task", back_populates="job", cascade="all,delete")
    vendor = Column(String(SMALL_STRING_LENGTH), default="")
    operating_system = Column(String(SMALL_STRING_LENGTH), default="")
    waiting_time = Column(Integer, default=0)
    creator = Column(String(SMALL_STRING_LENGTH), default="admin")
    push_to_git = Column(Boolean, default=False)
    workflows = relationship(
        "Workflow", secondary=job_workflow_table, back_populates="jobs"
    )
    python_query = Column(String(SMALL_STRING_LENGTH), default="")
    query_property_type = Column(String(SMALL_STRING_LENGTH), default="ip_address")
    devices = relationship("Device", secondary=job_device_table, back_populates="jobs")
    pools = relationship("Pool", secondary=job_pool_table, back_populates="jobs")
    events = relationship("Event", secondary=job_event_table, back_populates="jobs")
    send_notification = Column(Boolean, default=False)
    send_notification_method = Column(
        String(SMALL_STRING_LENGTH), default="mail_feedback_notification"
    )
    notification_header = Column(Text(LARGE_STRING_LENGTH), default="")
    display_only_failed_nodes = Column(Boolean, default=True)
    include_link_in_summary = Column(Boolean, default=True)
    mail_recipient = Column(String(SMALL_STRING_LENGTH), default="")
    shape = Column(String(SMALL_STRING_LENGTH), default="box")
    size = Column(Integer, default=40)
    color = Column(String(SMALL_STRING_LENGTH), default="#D2E5FF")
    initial_payload = Column(MutableDict.as_mutable(CustomMediumBlobPickle), default={})
    custom_username = Column(String(SMALL_STRING_LENGTH), default="")
    custom_password = Column(String(SMALL_STRING_LENGTH), default="")
    start_new_connection = Column(Boolean, default=False)
    runs = relationship("Run", back_populates="job")

    @property
    def filename(self) -> str:
        return controller.strip_all(self.name)

    def adjacent_jobs(
        self, workflow: "Workflow", direction: str, subtype: str
    ) -> Generator["Job", None, None]:
        for x in getattr(self, f"{direction}s"):
            if x.subtype == subtype and x.workflow == workflow:
                yield getattr(x, direction)

    def git_push(self, results: dict) -> None:
        path_git_folder = Path.cwd() / "git" / "automation"
        with open(path_git_folder / self.name, "w") as file:
            file.write(controller.str_dict(results))
        repo = Repo(str(path_git_folder))
        try:
            repo.git.add(A=True)
            repo.git.commit(m=f"Automatic commit ({self.name})")
        except GitCommandError:
            pass
        repo.remotes.origin.push()


class Service(Job):

    __tablename__ = "Service"
    __mapper_args__ = {"polymorphic_identity": "Service"}
    parent_cls = "Job"
    id = Column(Integer, ForeignKey("Job.id"), primary_key=True)
    multiprocessing = Column(Boolean, default=False)
    max_processes = Column(Integer, default=5)

    def device_run(
        self, run: "Run", payload: dict, targets: Optional[Set["Device"]] = None
    ) -> dict:
        if not targets:
            return run.get_results(payload)
        else:
            if run["multiprocessing"]:
                device_results: dict = {}
                thread_lock = Lock()
                processes = min(len(targets), run["max_processes"])
                args = (
                    run.runtime,
                    payload,
                    thread_lock,
                    device_results,
                )  # type: ignore
                process_args = [(device.id, *args) for device in targets]
                pool = ThreadPool(processes=processes)
                pool.map(get_device_result, process_args)
                pool.close()
                pool.join()
            else:
                device_results = {
                    device.name: run.get_results(payload, device) for device in targets
                }
            results = {"devices": device_results}
            return results

    def build_results(self, run: "Run", payload: dict, *other: Any) -> dict:
        results: dict = {"results": {}, "success": False, "runtime": run.runtime}
        targets = {}
        if run["has_targets"]:
            try:
                targets = run.compute_devices()
                results["results"]["devices"] = {}
            except Exception as exc:
                return {"success": False, "error": str(exc)}
        for i in range(run["number_of_retries"] + 1):
            run.log("info", f"Running {self.type} {self.name} (attempt n°{i + 1})")
            controller.job_db[run.runtime]["completed"] = 0
            controller.job_db[run.runtime]["failed"] = 0
            if not run.workflow:
                Session.commit()
            attempt = self.device_run(run, payload, targets)
            Session.commit()
            if targets:
                assert targets is not None
                for device in set(targets):
                    if not attempt["devices"][device.name]["success"]:
                        continue
                    results["results"]["devices"][device.name] = attempt["devices"][
                        device.name
                    ]
                    targets.remove(device)
                if not targets:
                    results["success"] = True
                    break
                else:
                    if run["number_of_retries"]:
                        results[f"Attempt {i + 1}"] = attempt
                    if i != run["number_of_retries"]:
                        sleep(run["time_between_retries"])
                    else:
                        for device in targets:
                            results["results"]["devices"][device.name] = attempt[
                                "devices"
                            ][device.name]
            else:
                if run["number_of_retries"]:
                    results[f"Attempts {i + 1}"] = attempt
                if attempt["success"] or i == run["number_of_retries"]:
                    results["results"] = attempt
                    results["success"] = attempt["success"]
                    break
                else:
                    sleep(run["time_between_retries"])
        return results

    def generate_row(self, table: str) -> List[str]:
        return [
            f"""<button type="button" class="btn btn-info btn-xs"
            onclick="showResultsPanel('{self.id}', '{self.name}', 'service')">
            </i>Results</a></button>""",
            f"""<button type="button" class="btn btn-success btn-xs"
            onclick="showTypePanel('{self.type}', '{self.id}', 'run')">Run</button>""",
            f"""<button type="button" class="btn btn-primary btn-xs"
            onclick="showTypePanel('{self.type}', '{self.id}')">Edit</button>""",
            f"""<button type="button" class="btn btn-primary btn-xs"
            onclick="showTypePanel('{self.type}', '{self.id}', 'duplicate')">
            Duplicate</button>""",
            f"""<button type="button" class="btn btn-primary btn-xs"
            onclick="exportJob('{self.id}')">
            Export</button>""",
            f"""<button type="button" class="btn btn-danger btn-xs"
            onclick="showDeletionPanel('service', '{self.id}', '{self.name}')">
            Delete</button>""",
        ]

    def space_deleter(self, input: str) -> str:
        return "".join(input.split())

    def sub(self, input: Any, variables: dict) -> dict:
        r = compile("{{(.*?)}}")

        def replace(match: Match) -> str:
            try:
                return str(eval(match.group()[2:-2], variables))
            except AttributeError:
                raise Exception(
                    "The variable subtitution mechanism failed."
                    " If you are using the 'device' variable, "
                    "check that the service has targets."
                )
            except NameError:
                raise Exception(
                    "The variable subtitution mechanism failed."
                    " Check that all variables are defined."
                )

        def rec(input: Any) -> Any:
            if isinstance(input, str):
                return r.sub(replace, input)
            elif isinstance(input, list):
                return [rec(x) for x in input]
            elif isinstance(input, dict):
                return {rec(k): rec(v) for k, v in input.items()}
            else:
                return input

        return rec(input)


class Workflow(Job):

    __tablename__ = "Workflow"
    __mapper_args__ = {"polymorphic_identity": "Workflow"}
    parent_cls = "Job"
    has_targets = Column(Boolean, default=True)
    id = Column(Integer, ForeignKey("Job.id"), primary_key=True)
    use_workflow_targets = Column(Boolean, default=True)
    jobs = relationship("Job", secondary=job_workflow_table, back_populates="workflows")
    edges = relationship(
        "WorkflowEdge", back_populates="workflow", cascade="all, delete-orphan"
    )

    def __init__(self, **kwargs: Any) -> None:
        end = fetch("Service", name="End")
        default = [fetch("Service", name="Start"), end]
        self.jobs.extend(default)
        super().__init__(**kwargs)
        if self.name not in end.positions:
            end.positions[self.name] = (500, 0)

    def generate_row(self, table: str) -> List[str]:
        return [
            f"""<button type="button" class="btn btn-info btn-xs"
            onclick="showResultsPanel('{self.id}', '{self.name}', 'workflow')">
            </i>Results</a></button>""",
            f"""<button type="button" class="btn btn-success btn-xs"
            onclick="showTypePanel('{self.type}', '{self.id}', 'run')">Run</button>""",
            f"""<button type="button" class="btn btn-primary btn-xs"
            onclick="showTypePanel('workflow', '{self.id}')">
            Edit</button>""",
            f"""<button type="button" class="btn btn-primary btn-xs"
            onclick="showTypePanel('workflow', '{self.id}', 'duplicate')">
            Duplicate</button>""",
            f"""<button type="button" class="btn btn-primary btn-xs"
            onclick="exportJob('{self.id}')">
            Export</button>""",
            f"""<button type="button" class="btn btn-danger btn-xs"
            onclick="showDeletionPanel('workflow', '{self.id}', '{self.name}')">
            Delete</button>""",
        ]

    def compute_valid_devices(
        self, run: Run, job: Job, allowed_devices: dict, payload: dict
    ) -> Set[Device]:
        if job.type != "Workflow" and not job.has_targets:
            return set()
        elif run["use_workflow_targets"]:
            return allowed_devices[job.name]
        else:
            return run.compute_devices(payload)

    def workflow_targets_processing(
        self, allowed_devices: dict, job: Job, results: dict
    ) -> Generator[Job, None, None]:

        failed_devices, passed_devices = set(), set()
        if job.type == "Workflow" or job.has_targets:
            if job.type == "Workflow":
                devices = results.get("devices", {})
            else:
                devices = results["results"].get("devices", {})
            for name, device_results in devices.items():
                if device_results["success"]:
                    passed_devices.add(fetch("Device", name=name))
                else:
                    failed_devices.add(fetch("Device", name=name))
        else:
            if results["success"]:
                passed_devices = allowed_devices[job.name]
            else:
                failed_devices = allowed_devices[job.name]
        for devices, edge in ((passed_devices, "success"), (failed_devices, "failure")):
            if not devices:
                continue
            for successor in job.adjacent_jobs(self, "destination", edge):
                allowed_devices[successor.name] |= devices
                yield successor

    def build_results(
        self, run: "Run", payload: dict, start_points: Optional[List["Job"]] = None
    ) -> dict:
        self.state = {"jobs": {}}
        jobs: list = start_points or [self.jobs[0]]
        payload = deepcopy(payload)
        visited: Set = set()
        results: dict = {"results": {}, "success": False, "runtime": run.runtime}
        allowed_devices: dict = defaultdict(set)
        if run["use_workflow_targets"]:
            initial_targets = run.compute_devices(results["results"])
            for job in jobs:
                allowed_devices[job.name] = initial_targets
        while jobs:
            job = jobs.pop()
            if any(
                node not in visited
                for node in job.adjacent_jobs(self, "source", "prerequisite")
            ):
                continue
            visited.add(job)
            self.state["current_job"] = job.get_properties()
            Session.commit()
            if run["use_workflow_targets"] and job.python_query:
                device_results, success = {}, True
                for base_target in allowed_devices[job.name]:
                    try:
                        derived_targets = run.compute_devices(payload, base_target)
                        job_run = factory(
                            "Run",
                            **{
                                "job": job.id,
                                "targets": [t.id for t in derived_targets],
                                "workflow": self.id,
                                "parent_runtime": run.parent_runtime,
                            },
                        )
                        derived_target_result = job_run.run(payload)
                        device_results[base_target.name] = derived_target_result
                        if not derived_target_result["success"]:
                            success = False
                    except Exception as exc:
                        device_results[base_target.name] = {
                            "success": False,
                            "error": str(exc),
                        }
                job_results = {
                    "results": {"devices": device_results},
                    "success": success,
                }
            else:
                valid_devices = self.compute_valid_devices(
                    run, job, allowed_devices, payload
                )
                job_run = factory(
                    "Run",
                    job=job.id,
                    workflow=self.id,
                    parent_runtime=run.parent_runtime,
                )
                job_run.properties = {"devices": [d.id for d in valid_devices]}
                job_results = job_run.run(payload)[0]
            self.state["jobs"][job.id] = job_results["success"]
            if run["use_workflow_targets"]:
                successors = self.workflow_targets_processing(
                    allowed_devices, job, job_results
                )
            else:
                successors = job.adjacent_jobs(
                    self,
                    "destination",
                    "success" if job_results["success"] else "failure",
                )
            payload[job.name] = job_results
            results["results"].update(payload)
            for successor in successors:
                if successor not in visited:
                    jobs.append(successor)
                if not run["use_workflow_targets"] and successor == self.jobs[1]:
                    results["success"] = True
            sleep(job.waiting_time)
        if run["use_workflow_targets"]:
            end_devices = allowed_devices["End"]
            results["devices"] = {
                device.name: {"success": device in end_devices}
                for device in initial_targets
            }
            results["success"] = initial_targets == end_devices
        return results


class WorkflowEdge(AbstractBase):

    __tablename__ = type = "WorkflowEdge"
    id = Column(Integer, primary_key=True)
    name = Column(String(SMALL_STRING_LENGTH), default="")
    subtype = Column(String(SMALL_STRING_LENGTH), default="")
    source_id = Column(Integer, ForeignKey("Job.id"))
    source = relationship(
        "Job",
        primaryjoin="Job.id == WorkflowEdge.source_id",
        backref=backref("destinations", cascade="all, delete-orphan"),
        foreign_keys="WorkflowEdge.source_id",
    )
    destination_id = Column(Integer, ForeignKey("Job.id"))
    destination = relationship(
        "Job",
        primaryjoin="Job.id == WorkflowEdge.destination_id",
        backref=backref("sources", cascade="all, delete-orphan"),
        foreign_keys="WorkflowEdge.destination_id",
    )
    workflow_id = Column(Integer, ForeignKey("Workflow.id"))
    workflow = relationship(
        "Workflow", back_populates="edges", foreign_keys="WorkflowEdge.workflow_id"
    )

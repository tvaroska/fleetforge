"""Unified FleetForge ORM schema barrel (`A-11`)."""

from fleetforge.db.base import Base as Base
from fleetforge.db.models_artifacts import Artifact as Artifact
from fleetforge.db.models_artifacts import ArtifactDelta as ArtifactDelta
from fleetforge.db.models_artifacts import ArtifactKind as ArtifactKind
from fleetforge.db.models_artifacts import ArtifactType as ArtifactType
from fleetforge.db.models_artifacts import ArtifactVersion as ArtifactVersion
from fleetforge.db.models_artifacts import Build as Build
from fleetforge.db.models_artifacts import BuildJob as BuildJob
from fleetforge.db.models_core import _DEFAULT_PROJECT_UUID_SQL as _DEFAULT_PROJECT_UUID_SQL
from fleetforge.db.models_core import DEFAULT_PROJECT_ID as DEFAULT_PROJECT_ID
from fleetforge.db.models_core import DEFAULT_PROJECT_SLUG as DEFAULT_PROJECT_SLUG
from fleetforge.db.models_core import AdminToken as AdminToken
from fleetforge.db.models_core import Project as Project
from fleetforge.db.models_core import ProjectMember as ProjectMember
from fleetforge.db.models_core import ProjectRole as ProjectRole
from fleetforge.db.models_core import ProjectSecret as ProjectSecret
from fleetforge.db.models_core import TimestampTZ as TimestampTZ
from fleetforge.db.models_core import User as User
from fleetforge.db.models_devices import AckResult as AckResult
from fleetforge.db.models_devices import CommandType as CommandType
from fleetforge.db.models_devices import Device as Device
from fleetforge.db.models_devices import DeviceCommand as DeviceCommand
from fleetforge.db.models_devices import DeviceFault as DeviceFault
from fleetforge.db.models_devices import DeviceGroup as DeviceGroup
from fleetforge.db.models_devices import DeviceLog as DeviceLog
from fleetforge.db.models_devices import DeviceMetric as DeviceMetric
from fleetforge.db.models_devices import DeviceProgress as DeviceProgress
from fleetforge.db.models_devices import DeviceTelemetry as DeviceTelemetry
from fleetforge.db.models_devices import EnrollmentToken as EnrollmentToken
from fleetforge.db.models_devices import GatewayRelayState as GatewayRelayState
from fleetforge.db.models_devices import LinkType as LinkType
from fleetforge.db.models_devices import PartitionProfile as PartitionProfile
from fleetforge.db.models_devices import PowerClass as PowerClass
from fleetforge.db.models_devices import ProgressStage as ProgressStage
from fleetforge.db.models_rollouts import TERMINAL_DEPLOY_STATES as TERMINAL_DEPLOY_STATES
from fleetforge.db.models_rollouts import DeployEvent as DeployEvent
from fleetforge.db.models_rollouts import DeployPolicy as DeployPolicy
from fleetforge.db.models_rollouts import DeployPolicyRun as DeployPolicyRun
from fleetforge.db.models_rollouts import DeployState as DeployState
from fleetforge.db.models_rollouts import ProjectRollout as ProjectRollout

__all__ = [
    "DEFAULT_PROJECT_ID",
    "DEFAULT_PROJECT_SLUG",
    "TERMINAL_DEPLOY_STATES",
    "_DEFAULT_PROJECT_UUID_SQL",
    "AckResult",
    "AdminToken",
    "Artifact",
    "ArtifactDelta",
    "ArtifactKind",
    "ArtifactType",
    "ArtifactVersion",
    "Base",
    "Build",
    "BuildJob",
    "CommandType",
    "DeployEvent",
    "DeployPolicy",
    "DeployPolicyRun",
    "DeployState",
    "Device",
    "DeviceCommand",
    "DeviceFault",
    "DeviceGroup",
    "DeviceLog",
    "DeviceMetric",
    "DeviceProgress",
    "DeviceTelemetry",
    "EnrollmentToken",
    "GatewayRelayState",
    "LinkType",
    "PartitionProfile",
    "PowerClass",
    "ProgressStage",
    "Project",
    "ProjectMember",
    "ProjectRole",
    "ProjectRollout",
    "ProjectSecret",
    "TimestampTZ",
    "User",
]

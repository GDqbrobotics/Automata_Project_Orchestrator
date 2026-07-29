from typing import Literal

WorkflowCommand = Literal[
    "start_workflow",
    "pause_workflow",
    "resume_workflow",
    "stop_workflow",
    "request_status",
]

WorkflowEvent = Literal[
    "workflow_state_changed",
    "subsystem_status_updated",
    "error_reported",
    "progress_updated",
]

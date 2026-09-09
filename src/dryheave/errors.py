class DryheaveError(Exception):
    code = "dryheave_error"
    exit_code = 1


class InputError(DryheaveError):
    code = "invalid_input"
    exit_code = 2


class IntegrityError(DryheaveError):
    code = "integrity_error"


class NotFoundError(DryheaveError):
    code = "not_found"


class ConflictError(DryheaveError):
    code = "conflict"


class LockBusyError(ConflictError):
    code = "lock_busy"


class PathError(InputError):
    code = "unsafe_path"


class LimitError(InputError):
    code = "limit_exceeded"


class CancelledError(DryheaveError):
    code = "cancelled"
    exit_code = 130

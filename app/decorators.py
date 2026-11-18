from functools import wraps

from flask import abort
from flask_login import current_user, login_required


def superadmin_required(fn):
    @wraps(fn)
    @login_required
    def wrapper(*args, **kwargs):
        if not current_user.can_manage_accounts:
            abort(403)
        return fn(*args, **kwargs)

    return wrapper


def lab_manager_required(fn):
    @wraps(fn)
    @login_required
    def wrapper(*args, **kwargs):
        if not current_user.can_manage_labs:
            abort(403)
        return fn(*args, **kwargs)

    return wrapper


# Back-compat alias: previously meant "system admin". Lab routes now use lab_manager_required.
admin_required = lab_manager_required


def reviewer_required(fn):
    @wraps(fn)
    @login_required
    def wrapper(*args, **kwargs):
        if not current_user.can_review:
            abort(403)
        return fn(*args, **kwargs)

    return wrapper


def stats_required(fn):
    @wraps(fn)
    @login_required
    def wrapper(*args, **kwargs):
        if not current_user.can_view_stats:
            abort(403)
        return fn(*args, **kwargs)

    return wrapper

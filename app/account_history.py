"""Retain audit evidence when deleting an account with no business records."""
from app.models import (AuditLog, Blacklist, LoginLog, SecurityEvent, SecurityWhitelist,
                        MaterialClaim, MaterialPurchase, EmailVerification)


def detach_account_history(user_id):
    # Snapshot identifiers/names remain in the logs; only nullable foreign keys detach.
    for model, columns in [
        (LoginLog, ['user_id']), (AuditLog, ['actor_id','undone_by_id']),
        (Blacklist, ['created_by_id','lifted_by_id']), (SecurityEvent,['user_id']),
        (SecurityWhitelist,['created_by_id']),
        (MaterialClaim, ['updated_by_id']),
        (MaterialPurchase, ['updated_by_id', 'settled_by_id']),
    ]:
        for name in columns:
            column=getattr(model,name)
            model.query.filter(column==user_id).update({name:None},synchronize_session='fetch')
    EmailVerification.query.filter_by(user_id=user_id).delete(synchronize_session='fetch')

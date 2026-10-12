"""Find out which AWS accounts to scan."""

import logging

from ebs_cleanup.aws import make_client
from ebs_cleanup.models import Account

log = logging.getLogger(__name__)


def list_organization_accounts(session):
    """All ACTIVE accounts in the AWS Organization (run this from the management account)."""
    organizations = make_client(session, "organizations")
    accounts = []
    for page in organizations.get_paginator("list_accounts").paginate():
        for item in page["Accounts"]:
            if item["Status"] == "ACTIVE":  # skip suspended / closing accounts
                accounts.append(Account(account_id=item["Id"], name=item["Name"]))
    return accounts


def select_accounts(accounts, include, exclude):
    """Apply the --include-accounts / --exclude-accounts lists."""
    chosen = accounts
    if include:
        known = {a.account_id for a in accounts}
        for missing in sorted(set(include) - known):
            log.warning("Account %s was asked for but is not an active account in the organization", missing)
        chosen = [a for a in chosen if a.account_id in include]
    return [a for a in chosen if a.account_id not in exclude]

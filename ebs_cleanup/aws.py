"""Helpers for creating boto3 sessions and clients."""

import boto3
from botocore.config import Config

# "standard" retry mode waits and retries by itself when AWS answers "slow down".
_BOTO_CONFIG = Config(retries={"mode": "standard", "max_attempts": 10})


def make_session(profile="", region=""):
    """A boto3 session. Empty strings mean "use the normal AWS defaults"."""
    return boto3.Session(profile_name=profile or None, region_name=region or None)


def make_client(session, service, region=None):
    return session.client(service, region_name=region, config=_BOTO_CONFIG)


def whoami(session):
    """Returns a dict with 'Account' and 'Arn' for the current credentials."""
    return make_client(session, "sts").get_caller_identity()


def partition_of(arn):
    """arn:aws:iam::123456789012:user/x -> 'aws'. Other partitions: aws-us-gov, aws-cn."""
    return arn.split(":")[1]


def session_for_account(
    session, account_id, role_name, partition, external_id="", session_name="ebs-cleanup", region=""
):
    """Assume a role in another account and return a session that uses it."""
    role_arn = f"arn:{partition}:iam::{account_id}:role/{role_name}"
    request = {"RoleArn": role_arn, "RoleSessionName": session_name}
    if external_id:
        request["ExternalId"] = external_id
    creds = make_client(session, "sts").assume_role(**request)["Credentials"]
    return boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=region or session.region_name,
    )

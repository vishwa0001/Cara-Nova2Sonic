"""
cleanup_careharmony_account.py
Deletes ALL Careharmony/TalkingBot project resources from AWS account 701348334422 (us-east-1).
Profile: careharmony — ONLY this profile is touched.

Usage:
  python cleanup_careharmony_account.py --dry-run   # list what will be deleted
  python cleanup_careharmony_account.py              # actually delete
"""

import boto3
import argparse
import sys

# Reconfigure stdout/stderr for Windows console unicode safety
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# -- Config --------------------------------------------------------------------

PROFILE   = "careharmony"
REGION    = "us-east-1"
ACCOUNT   = "701348334422"

EVENTBRIDGE_RULES = [
    "cara-health-bot-campaign-disconnected",
    "talking-bot-dial-scheduler-dev",
]

LAMBDA_FUNCTIONS = [
    "cara-health-bot-campaign-api",
    "cara-health-bot-campaign-dialer",
    "cara-health-bot-campaign-intake",
    "cara-health-bot-agent-availability",
    "talking-bot-upload-url-dev",
    "talking-bot-list-dev",
    "talking-bot-ingest-dev",
    "talking-bot-reschedule-dev",
    "talking-bot-dial-scheduler-dev",
]

DYNAMODB_TABLES = [
    "TalkingBotPatientRecords-dev",
    "TalkingBotCallBatches-dev",
    "cara-health-bot-campaign-state",
]

S3_BUCKETS = [
    f"cara-health-bot-campaigns-{ACCOUNT}-{REGION}",
    f"talking-bot-patient-raw-dev-334422-{REGION}",
]

IAM_ROLES = [
    "cara-health-bot-campaign-api-role",
    "cara-health-bot-campaign-dialer-role",
    "cara-health-bot-campaign-intake-role",
    "cara-health-bot-campaign-scheduler-role",
    "TalkingBotCallingLambdaRole-dev",
    "TalkingBotIngestLambdaRole-dev",
    "TalkingBotListLambdaRole-dev",
    "TalkingBotRescheduleLambdaRole-dev",
    "TalkingBotUploadUrlLambdaRole-dev",
]

CW_LOG_GROUPS = [
    "/aws/lambda/cara-health-bot-campaign-api",
    "/aws/lambda/cara-health-bot-campaign-dialer",
    "/aws/lambda/cara-health-bot-campaign-intake",
    "/aws/lambda/cara-health-bot-agent-availability",
    "/aws/lambda/cara-health-bot-session-context",
    "/aws/lambda/talking-bot-campaign-dialer",
    "/aws/lambda/talking-bot-campaign-intake",
    "/aws/lambda/talking-bot-dial-scheduler-dev",
    "/aws/lambda/talking-bot-ingest-dev",
    "/aws/lambda/talking-bot-list-dev",
    "/aws/lambda/talking-bot-reschedule-dev",
    "/aws/lambda/talking-bot-session-context",
    "/aws/lambda/talking-bot-upload-url-dev",
    "/aws/connect/cara-health-bot-334422",
    "/aws/connect/talking-bot-334422",
]

LEX_BOTS = [
    {"id": "DBAZBTDO5O", "name": "cara-health-bot-identity"},
    {"id": "YJJPZ85M0A", "name": "cara_bot"},
    {"id": "CNZMH20HTS", "name": "cara-test-bot"},
]

# ── Helpers ───────────────────────────────────────────────────────────────────

def log(action, resource, dry=False):
    prefix = "[DRY-RUN]" if dry else "[DELETED]"
    print(f"{prefix} {action}: {resource}")

def warn(resource, reason):
    print(f"[SKIP]    {resource} — {reason}")

# ── Session ───────────────────────────────────────────────────────────────────

def make_session():
    session = boto3.Session(profile_name=PROFILE, region_name=REGION)
    sts = session.client("sts")
    identity = sts.get_caller_identity()
    actual_account = identity["Account"]
    if actual_account != ACCOUNT:
        print(f"ERROR: Expected account {ACCOUNT}, got {actual_account}. Aborting.")
        sys.exit(1)
    print(f"Account verified: {actual_account} | Profile: {PROFILE} | Region: {REGION}\n")
    return session

# ── 1. EventBridge ────────────────────────────────────────────────────────────

def delete_eventbridge_rules(session, dry):
    eb = session.client("events")
    for rule in EVENTBRIDGE_RULES:
        try:
            targets = eb.list_targets_by_rule(Rule=rule).get("Targets", [])
            if targets:
                ids = [t["Id"] for t in targets]
                if not dry:
                    eb.remove_targets(Rule=rule, Ids=ids)
                log("EventBridge remove targets", f"{rule} → {ids}", dry)
            if not dry:
                eb.delete_rule(Name=rule, Force=True)
            log("EventBridge rule", rule, dry)
        except eb.exceptions.ResourceNotFoundException:
            warn(rule, "not found")
        except Exception as e:
            warn(rule, str(e))

# ── 2. Lambda ─────────────────────────────────────────────────────────────────

def delete_lambda_functions(session, dry):
    lmb = session.client("lambda")
    for fn in LAMBDA_FUNCTIONS:
        try:
            # Remove function URL if exists
            try:
                if not dry:
                    lmb.delete_function_url_config(FunctionName=fn)
                log("Lambda URL config", fn, dry)
            except lmb.exceptions.ResourceNotFoundException:
                pass

            if not dry:
                lmb.delete_function(FunctionName=fn)
            log("Lambda function", fn, dry)
        except lmb.exceptions.ResourceNotFoundException:
            warn(fn, "not found")
        except Exception as e:
            warn(fn, str(e))

# ── 3. DynamoDB ───────────────────────────────────────────────────────────────

def delete_dynamodb_tables(session, dry):
    ddb = session.client("dynamodb")
    for table in DYNAMODB_TABLES:
        try:
            if not dry:
                ddb.delete_table(TableName=table)
            log("DynamoDB table", table, dry)
        except ddb.exceptions.ResourceNotFoundException:
            warn(table, "not found")
        except Exception as e:
            warn(table, str(e))

# ── 4. S3 ─────────────────────────────────────────────────────────────────────

def delete_s3_buckets(session, dry):
    s3 = session.resource("s3")
    for bucket_name in S3_BUCKETS:
        try:
            bucket = s3.Bucket(bucket_name)
            if not dry:
                # Delete all object versions (handles versioned buckets)
                bucket.object_versions.delete()
                # Delete any remaining objects
                bucket.objects.delete()
                bucket.delete()
            log("S3 bucket (all objects + bucket)", bucket_name, dry)
        except s3.meta.client.exceptions.NoSuchBucket:
            warn(bucket_name, "not found")
        except Exception as e:
            warn(bucket_name, str(e))

# ── 5. IAM ────────────────────────────────────────────────────────────────────

def delete_iam_roles(session, dry):
    iam = session.client("iam")
    for role in IAM_ROLES:
        try:
            # Detach managed policies
            paginator = iam.get_paginator("list_attached_role_policies")
            for page in paginator.paginate(RoleName=role):
                for policy in page["AttachedPolicies"]:
                    if not dry:
                        iam.detach_role_policy(RoleName=role, PolicyArn=policy["PolicyArn"])
                    log("IAM detach managed policy", f"{role} ← {policy['PolicyName']}", dry)

            # Delete inline policies
            paginator2 = iam.get_paginator("list_role_policies")
            for page in paginator2.paginate(RoleName=role):
                for policy_name in page["PolicyNames"]:
                    if not dry:
                        iam.delete_role_policy(RoleName=role, PolicyName=policy_name)
                    log("IAM delete inline policy", f"{role} / {policy_name}", dry)

            # Remove from instance profiles
            profiles = iam.list_instance_profiles_for_role(RoleName=role).get("InstanceProfiles", [])
            for profile in profiles:
                if not dry:
                    iam.remove_role_from_instance_profile(
                        InstanceProfileName=profile["InstanceProfileName"],
                        RoleName=role,
                    )

            if not dry:
                iam.delete_role(RoleName=role)
            log("IAM role", role, dry)
        except iam.exceptions.NoSuchEntityException:
            warn(role, "not found")
        except Exception as e:
            warn(role, str(e))

# ── 6. CloudWatch Logs ────────────────────────────────────────────────────────

def delete_log_groups(session, dry):
    logs = session.client("logs")
    for group in CW_LOG_GROUPS:
        try:
            if not dry:
                logs.delete_log_group(logGroupName=group)
            log("CloudWatch log group", group, dry)
        except logs.exceptions.ResourceNotFoundException:
            warn(group, "not found")
        except Exception as e:
            warn(group, str(e))

# ── 7. Lex V2 Bots ────────────────────────────────────────────────────────────

def delete_lex_bots(session, dry):
    lex = session.client("lexv2-models")
    for bot in LEX_BOTS:
        try:
            # Delete all aliases first (except TSTALIASID which is auto-managed)
            aliases = lex.list_bot_aliases(botId=bot["id"]).get("botAliasSummaries", [])
            for alias in aliases:
                if alias["botAliasId"] == "TSTALIASID":
                    continue
                if not dry:
                    lex.delete_bot_alias(
                        botId=bot["id"],
                        botAliasId=alias["botAliasId"],
                        skipResourceInUseCheck=True,
                    )
                log("Lex alias", f"{bot['name']} / {alias['botAliasName']}", dry)

            # Delete all versions
            versions = lex.list_bot_versions(botId=bot["id"]).get("botVersionSummaries", [])
            for version in versions:
                if version["botVersion"] == "DRAFT":
                    continue
                if not dry:
                    lex.delete_bot_version(
                        botId=bot["id"],
                        botVersion=version["botVersion"],
                        skipResourceInUseCheck=True,
                    )
                log("Lex version", f"{bot['name']} v{version['botVersion']}", dry)

            if not dry:
                lex.delete_bot(botId=bot["id"], skipResourceInUseCheck=True)
            log("Lex bot", f"{bot['name']} ({bot['id']})", dry)
        except lex.exceptions.ResourceNotFoundException:
            warn(bot["name"], "not found")
        except Exception as e:
            warn(bot["name"], str(e))

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Preview only — nothing deleted")
    args = parser.parse_args()
    dry = args.dry_run

    if dry:
        print("=" * 60)
        print("DRY-RUN MODE — no resources will be deleted")
        print("=" * 60 + "\n")
    else:
        print("=" * 60)
        print("LIVE DELETE MODE — resources will be permanently deleted")
        confirm = input("Type 'yes' to confirm: ").strip().lower()
        if confirm != "yes":
            print("Aborted.")
            sys.exit(0)
        print("=" * 60 + "\n")

    session = make_session()

    print("── 1. EventBridge Rules ──")
    delete_eventbridge_rules(session, dry)

    print("\n── 2. Lambda Functions ──")
    delete_lambda_functions(session, dry)

    print("\n── 3. DynamoDB Tables ──")
    delete_dynamodb_tables(session, dry)

    print("\n── 4. S3 Buckets ──")
    delete_s3_buckets(session, dry)

    print("\n── 5. IAM Roles ──")
    delete_iam_roles(session, dry)

    print("\n── 6. CloudWatch Log Groups ──")
    delete_log_groups(session, dry)

    print("\n── 7. Lex V2 Bots ──")
    delete_lex_bots(session, dry)

    print("\n" + "=" * 60)
    print("Done." if not dry else "Dry-run complete. Run without --dry-run to delete.")
    print("=" * 60)

if __name__ == "__main__":
    main()

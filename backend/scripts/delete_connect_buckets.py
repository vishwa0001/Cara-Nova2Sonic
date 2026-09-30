"""
delete_connect_buckets.py
Empties and deletes 12 S3 buckets from careharmony profile (us-east-1).

Usage:
  python delete_connect_buckets.py --dry-run
  python delete_connect_buckets.py
"""

import boto3
import argparse
import sys

# Reconfigure stdout/stderr for Windows console unicode safety
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

PROFILE = "careharmony"
REGION  = "us-east-1"

BUCKETS = [
    "ai-voice-agent-test-bkt",
    "amazon-connect-5753fcd26aa6-access-logs",
    "amazon-connect-7008a466c5c0",
    "amazon-connect-7008a466c5c0-access-logs",
    "amazon-connect-89a8d6b2f616",
    "amazon-connect-89a8d6b2f616-access-logs",
    "amazon-connect-c94e5704a0fc",
    "amazon-connect-c94e5704a0fc-access-logs",
    "amazon-connect-cc0e9fbf62bf",
    "amazon-connect-cc0e9fbf62bf-access-logs",
    "amazon-connect-ffe294485313",
    "amazon-connect-ffe294485313-access-logs",
]

def delete_bucket(s3, bucket_name, dry):
    try:
        bucket = s3.Bucket(bucket_name)

        # Purge all object versions (handles versioning-enabled buckets)
        versions = list(bucket.object_versions.all())
        if versions:
            if not dry:
                bucket.object_versions.delete()
            print(f"  [{'DRY' if dry else 'OK'}] Purged {len(versions)} versions — {bucket_name}")
        else:
            # Also check regular objects if not versioned
            objects = list(bucket.objects.all())
            if objects:
                if not dry:
                    bucket.objects.delete()
                print(f"  [{'DRY' if dry else 'OK'}] Purged {len(objects)} objects — {bucket_name}")
            else:
                print(f"  [{'DRY' if dry else 'OK'}] No objects — {bucket_name}")

        # Delete bucket
        if not dry:
            bucket.delete()
        print(f"  [{'DRY' if dry else 'DELETED'}] Bucket — {bucket_name}")

    except s3.meta.client.exceptions.NoSuchBucket:
        print(f"  [SKIP] Not found — {bucket_name}")
    except Exception as e:
        print(f"  [ERROR] {bucket_name} — {e}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    dry = args.dry_run

    print("=" * 60)
    if dry:
        print("DRY-RUN — nothing will be deleted")
    else:
        print("LIVE DELETE — buckets will be permanently deleted")
        confirm = input("Type 'yes' to confirm: ").strip().lower()
        if confirm != "yes":
            print("Aborted.")
            sys.exit(0)
    print("=" * 60 + "\n")

    session = boto3.Session(profile_name=PROFILE, region_name=REGION)
    s3 = session.resource("s3")

    for bucket in BUCKETS:
        delete_bucket(s3, bucket, dry)

    print("\n" + "=" * 60)
    print("Done." if not dry else "Dry-run complete. Run without --dry-run to delete.")
    print("=" * 60)

if __name__ == "__main__":
    main()

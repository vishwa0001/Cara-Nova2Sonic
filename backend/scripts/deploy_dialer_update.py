"""Deploy the updated Campaign Dialer Lambda with Sequential/Parallel mode support."""
import boto3
import io
import os
import sys
import zipfile

def main():
    profile = os.environ.get("AWS_PROFILE", "careharmony")
    region = os.environ.get("AWS_REGION", "us-east-1")
    
    try:
        session = boto3.Session(profile_name=profile, region_name=region)
        lmb = session.client("lambda")
    except Exception as e:
        print(f"AWS Session init error ({profile}): {e}")
        session = boto3.Session(region_name=region)
        lmb = session.client("lambda")

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    
    # 1. Read config.json
    config_path = os.path.join(root, "config.json")
    with open(config_path, "r", encoding="utf-8") as f:
        config_data = f.read()

    # 2. Package ZIP
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        dialer_path = os.path.join(root, "lambda", "campaign_dialer.py")
        with open(dialer_path, "rb") as f:
            z.writestr("campaign_dialer.py", f.read())
        z.writestr("config.json", config_data.encode("utf-8"))
        outreach_path = os.path.join(root, "storage", "outreach_records.py")
        if os.path.exists(outreach_path):
            with open(outreach_path, "rb") as f:
                z.writestr("outreach_records.py", f.read())

    buf.seek(0)
    zip_bytes = buf.read()

    print(f"Uploading updated code to Lambda 'cara-campaign-dialer' ({len(zip_bytes)} bytes)...")
    try:
        lmb.update_function_code(
            FunctionName="cara-campaign-dialer",
            ZipFile=zip_bytes
        )
        print("Function code updated successfully. Waiting for update to finish...")
        waiter = lmb.get_waiter('function_updated')
        waiter.wait(FunctionName="cara-campaign-dialer")

        # Update environment variables
        cfg = lmb.get_function_configuration(FunctionName="cara-campaign-dialer")
        env_vars = cfg.get("Environment", {}).get("Variables", {})
        env_vars["DIALING_MODE"] = "SEQUENTIAL"
        env_vars["MAX_CONCURRENT_CALLS"] = "1"
        
        lmb.update_function_configuration(
            FunctionName="cara-campaign-dialer",
            Environment={"Variables": env_vars}
        )
        print("Function environment variables updated successfully: DIALING_MODE=SEQUENTIAL, MAX_CONCURRENT_CALLS=1")
    except Exception as e:
        print(f"Error updating AWS Lambda: {e}")
        print("Note: If your AWS SSO/CLI token has expired, please re-authenticate and run this script.")

if __name__ == "__main__":
    main()

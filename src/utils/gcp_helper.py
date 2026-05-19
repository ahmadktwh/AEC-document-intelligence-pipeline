import os
import json
import logging

logger = logging.getLogger("GCP-Helper")

def setup_gcp_credentials():
    """
    Sets up Google Cloud credentials for Vertex AI in a Lambda environment.
    Reads the JSON from GCP_SERVICE_ACCOUNT_JSON environment variable,
    saves it to /tmp/gcp_creds.json, and sets GOOGLE_APPLICATION_CREDENTIALS.
    """
    gcp_json = os.environ.get("GCP_SERVICE_ACCOUNT_JSON")
    
    if not gcp_json:
        # Check if local file exists (potentially baked into Docker)
        local_path = os.path.join(os.getcwd(), "service-account.json")
        if os.path.exists(local_path):
            logger.info(f"Using local service account file found at {local_path}")
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = local_path
            return True
        
        logger.warning("GCP_SERVICE_ACCOUNT_JSON not found and no local service-account.json detected.")
        return False
        
    try:
        # Define path in Lambda's writable /tmp directory
        creds_path = "/tmp/gcp_creds.json"
        
        # Write the JSON to the file
        with open(creds_path, "w") as f:
            f.write(gcp_json)
            
        # Set the environment variable that Google SDK looks for
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = creds_path
        
        logger.info(f"GCP Credentials successfully setup at {creds_path}")
        return True
    except Exception as e:
        logger.error(f"Failed to setup GCP credentials: {e}")
        return False

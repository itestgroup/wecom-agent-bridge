"""Update code only for the existing SCF; never send a WeCom message.

Requires scripts/requirements-deploy.txt. Normal use is GitHub Actions.
The HTTP URL must target $LATEST or $DEFAULT routing entirely to $LATEST.
This tool does not alter aliases,
versions, handler, environment, network, triggers, runtime, or layers.
"""
import ast
import base64
import hashlib
import io
import os
from pathlib import Path
import sys
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
REGION = os.environ.get("SCF_REGION", "")
NAMESPACE = os.environ.get("SCF_NAMESPACE", "")
FUNCTION = os.environ.get("SCF_FUNCTION_NAME", "")


def build_package(source_dir):
    source_dir = Path(source_dir)
    if not (source_dir / "index.py").is_file():
        raise ValueError("scf/index.py is required")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source_dir.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            if path.is_symlink() or any(p.is_symlink() for p in path.parents if p != source_dir.parent):
                raise ValueError("Symlinks cannot be deployed")
            relative = path.relative_to(source_dir).as_posix()
            if any(part.startswith(".") for part in Path(relative).parts):
                continue
            content = path.read_bytes()
            # The archived function requires Python 3.9+.
            ast.parse(content, filename=relative, feature_version=(3, 9))
            # Stable archive metadata makes package hashes reproducible.
            info = zipfile.ZipInfo(relative, (2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)
    package = output.getvalue()
    if len(package) > 19 * 1024 * 1024:
        raise ValueError("Deployment ZIP exceeds the configured safe size limit")
    return package


def create_client():
    from tencentcloud.common import credential
    from tencentcloud.common.profile.client_profile import ClientProfile
    from tencentcloud.common.profile.http_profile import HttpProfile
    from tencentcloud.scf.v20180416 import scf_client

    secret_id = os.environ.get("TENCENT_SECRET_ID", "")
    secret_key = os.environ.get("TENCENT_SECRET_KEY", "")
    if not secret_id or not secret_key:
        raise ValueError("Set TENCENT_SECRET_ID and TENCENT_SECRET_KEY in GitHub Secrets")
    cred = credential.Credential(secret_id, secret_key, os.environ.get("TENCENT_TOKEN") or None)
    http = HttpProfile(endpoint="scf.tencentcloudapi.com", reqTimeout=30)
    profile = ClientProfile(httpProfile=http)
    return scf_client.ScfClient(cred, REGION, profile)


def check_url_target(client, models, qualifier):
    if qualifier == "$LATEST":
        return
    if qualifier != "$DEFAULT":
        raise ValueError("SCF_URL_QUALIFIER must be $LATEST or $DEFAULT")
    req = models.GetAliasRequest()
    req.FunctionName, req.Namespace, req.Name = FUNCTION, NAMESPACE, "$DEFAULT"
    alias = client.GetAlias(req)
    if alias.FunctionVersion != "$LATEST":
        raise RuntimeError("$DEFAULT must have $LATEST as its primary version")
    routing = alias.RoutingConfig
    weights = getattr(routing, "AdditionalVersionWeights", None) or []
    matches = getattr(routing, "AddtionVersionMatchs", None) or []
    for entry in weights:
        weight = entry.Weight
        if not isinstance(weight, (int, float)) or not 0 <= weight <= 1:
            raise RuntimeError("Invalid $DEFAULT routing weight")
        if entry.Version != "$LATEST" and weight != 0:
            raise RuntimeError("$DEFAULT routes traffic to another version; deployment stopped")
    if any(entry.Version != "$LATEST" for entry in matches):
        raise RuntimeError("$DEFAULT has routing rules for another version; deployment stopped")


def deploy(client, models, package, sleep=time.sleep, attempts=60, qualifier="$LATEST"):
    check_url_target(client, models, qualifier)
    def inspect():
        req = models.GetFunctionRequest()
        req.FunctionName, req.Namespace = FUNCTION, NAMESPACE
        req.Qualifier = "$LATEST"
        req.ShowCode = "FALSE"
        return client.GetFunction(req)

    before = inspect()
    if before.Status != "Active":
        raise RuntimeError("SCF must be Active before deployment")
    if not str(before.Runtime).startswith("Python") or before.Handler != "index.main_handler":
        raise RuntimeError("Expected a Python event function with handler index.main_handler")

    req = models.UpdateFunctionCodeRequest()
    req.FunctionName, req.Namespace = FUNCTION, NAMESPACE
    req.CodeSource = "ZipFile"
    req.ZipFile = base64.b64encode(package).decode("ascii")
    req.Publish = "FALSE"
    # Do not print request objects or full GetFunction responses: they can expose secrets.
    response = client.UpdateFunctionCode(req)
    print("UpdateFunctionCode accepted; request_id=" + str(response.RequestId))
    digest = hashlib.sha256(package).digest()
    hashes = {digest.hex(), base64.b64encode(digest).decode("ascii")}
    for _ in range(attempts):
        sleep(3)
        result = inspect()
        if result.Status == "Active":
            address = models.GetFunctionAddressRequest()
            address.FunctionName, address.Namespace = FUNCTION, NAMESPACE
            address.Qualifier = "$LATEST"
            # Never log the signed download URL returned alongside the hash.
            remote_hash = client.GetFunctionAddress(address).CodeSha256
            if remote_hash in hashes:
                check_url_target(client, models, qualifier)
                print("SCF is Active and its package SHA256 matches. Message delivery still needs acceptance testing.")
                return
        if "fail" in str(result.Status).lower():
            raise RuntimeError("SCF reported a failed deployment state")
    raise RuntimeError("Timed out waiting for SCF Active/package match; inspect the console before retrying")


def main():
    if not all((REGION, NAMESPACE, FUNCTION)):
        raise ValueError("Set SCF_REGION, SCF_NAMESPACE and SCF_FUNCTION_NAME explicitly")
    qualifier = os.environ.get("SCF_URL_QUALIFIER")
    if qualifier not in ("$LATEST", "$DEFAULT"):
        raise ValueError("Confirm the function URL target, then set SCF_URL_QUALIFIER to $LATEST or $DEFAULT")
    package = build_package(ROOT / "scf")
    print("Target: " + REGION + "/" + NAMESPACE + "/" + FUNCTION)
    print("Package SHA256: " + hashlib.sha256(package).hexdigest())
    client = create_client()
    from tencentcloud.scf.v20180416 import models
    deploy(client, models, package, qualifier=qualifier)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # SDK exceptions may include request/response details; only emit their code.
        code = error.get_code() if hasattr(error, "get_code") else type(error).__name__
        detail = str(error) if isinstance(error, (ValueError, RuntimeError)) else "Check SCF/CAM settings and the deployment documentation."
        print("Deployment failed [" + str(code) + "]: " + detail, file=sys.stderr)
        sys.exit(1)

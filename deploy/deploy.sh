#!/bin/sh
# Publishes agent-link to the k8s cluster (namespace agent-link, NodePort 30110) via ubuntu03.
#
# The hub, the CLI and the installer ship together as one ConfigMap. Code changes roll the pod;
# messages live on a local-path PVC, so a rollout does not lose the queue.
set -e
cd "$(dirname "$0")/.."
python3 -m json.tool package.json >/dev/null 2>&1 || true

python3 - <<'PY' | ssh ubuntu03 'kubectl apply --server-side --force-conflicts -f - >/dev/null &&
  kubectl -n agent-link rollout status deploy/hub --timeout=120s'
import hashlib, json, pathlib

files = {name: pathlib.Path(name).read_text() for name in ("hub.py", "link.py", "install.sh")}
digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()[:16]
manifest = pathlib.Path("deploy/k8s.yaml").read_text().replace("__CODE_HASH__", digest)
docs = manifest.split("\n---\n")
cm = {"apiVersion": "v1", "kind": "ConfigMap",
      "metadata": {"name": "hub-code", "namespace": "agent-link"}, "data": files}
print(docs[0]); print("---")           # namespace
print(json.dumps(cm)); print("---")
print("\n---\n".join(docs[1:]))
PY
echo "Published: http://ubuntu03:30110/"

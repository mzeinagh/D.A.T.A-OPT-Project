#!/usr/bin/env bash
# Builds the web and worker images from the current commit and moves both
# Container Apps to them, so the two always run the same version of the code.
#
# Run from Azure Cloud Shell, in a clone of this repository checked out at
# the commit to deploy:
#
#   export RESOURCE_GROUP=HcSx-ToxDataAI-DampRaccoon-dataopt-rg
#   export ACR_NAME=<registry name>
#   ./scripts/azure/deploy.sh
#
# The very first time, before the container apps exist, run it with
# --build-only: it builds both images and prints their names, to pick when
# creating the apps.
#
# Optional: WEB_APP (default dataopt-web), WORKER_APP (default dataopt-worker).
# Full walkthrough: docs/AZURE_DEPLOYMENT.md.
set -euo pipefail

: "${RESOURCE_GROUP:?Set RESOURCE_GROUP to the resource group holding the container apps}"
: "${ACR_NAME:?Set ACR_NAME to the container registry name}"
WEB_APP="${WEB_APP:-dataopt-web}"
WORKER_APP="${WORKER_APP:-dataopt-worker}"
BUILD_ONLY=false
if [ "${1:-}" = "--build-only" ]; then
    BUILD_ONLY=true
fi

cd "$(git rev-parse --show-toplevel)"

# The image tag is the commit, so uncommitted edits would ship under a tag
# that doesn't describe them.
if [ -n "$(git status --porcelain)" ]; then
    echo "There are uncommitted changes. Commit or discard them first." >&2
    git status --short >&2
    exit 1
fi

TAG="$(git rev-parse --short=12 HEAD)"
REGISTRY="$(az acr show --name "$ACR_NAME" --query loginServer --output tsv)"

if [ "$BUILD_ONLY" = false ]; then
    # The commit currently running, read from the worker's image tag, to
    # spot database migrations added since then. Also fails early, before
    # the long builds, if the apps don't exist yet.
    PREVIOUS="$(az containerapp show --name "$WORKER_APP" --resource-group "$RESOURCE_GROUP" \
        --query 'properties.template.containers[0].image' --output tsv)"
    PREVIOUS="${PREVIOUS##*:}"
    echo "==> Deploying commit $TAG (worker currently on: $PREVIOUS)"
else
    echo "==> Building commit $TAG (no apps updated)"
fi

# Build both images before touching either app: if a build fails, nothing
# has changed and the two apps still match.
echo "==> Building the web image"
az acr build --registry "$ACR_NAME" --target web \
    --image "dataopt-web:$TAG" .

echo "==> Building the worker image (the slow one: PyTorch and model downloads)"
az acr build --registry "$ACR_NAME" --target worker --timeout 7200 \
    --image "dataopt-worker:$TAG" .

if [ "$BUILD_ONLY" = true ]; then
    echo "==> Built:"
    echo "    $REGISTRY/dataopt-web:$TAG"
    echo "    $REGISTRY/dataopt-worker:$TAG"
    exit 0
fi

# Worker first, so it already understands any new kind of job before the
# website starts sending it.
echo "==> Updating $WORKER_APP"
az containerapp update --name "$WORKER_APP" --resource-group "$RESOURCE_GROUP" \
    --image "$REGISTRY/dataopt-worker:$TAG" --output none

echo "==> Updating $WEB_APP"
az containerapp update --name "$WEB_APP" --resource-group "$RESOURCE_GROUP" \
    --image "$REGISTRY/dataopt-web:$TAG" --output none

echo "==> Now running:"
for app in "$WORKER_APP" "$WEB_APP"; do
    printf '    %-16s %s\n' "$app" "$(az containerapp show --name "$app" \
        --resource-group "$RESOURCE_GROUP" \
        --query 'properties.template.containers[0].image' --output tsv)"
done

if git cat-file -e "$PREVIOUS^{commit}" 2>/dev/null; then
    if [ -n "$(git diff --name-only "$PREVIOUS" HEAD -- '*/migrations/*')" ]; then
        echo "==> This deploy adds database migrations. Run them once from the" \
             "worker's Console: python manage.py migrate"
    fi
else
    echo "==> Couldn't tell whether this deploy adds database migrations." \
         "If unsure, run from the worker's Console: python manage.py migrate" \
         "(safe to run when there's nothing to apply)"
fi

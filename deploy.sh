#!/bin/bash

# InternetArchiveImageFetcher deploy helper

set -euo pipefail

DIR_NAME=$(basename "$PWD")
REGION="us-east-2"
S3_TEMP_BUCKET="biospex-loc"
ZIP_NAME="function.zip"
BUILD_DIR="build"
DOCKER_IMAGE="public.ecr.aws/lambda/python:3.12"

echo ">>> Starting deployment for: $DIR_NAME"

if [ ! -f "requirements.txt" ] || [ ! -f "lambda_function.py" ]; then
    echo "Error: requirements.txt and lambda_function.py are required"
    exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
    echo "Error: Docker is required for Lambda-compatible builds."
    echo "Start by installing Docker, then run: sudo service docker start"
    exit 1
fi

if ! docker info >/dev/null 2>&1; then
    echo "Error: Docker daemon is not running."
    echo "Start it only for this build with: sudo service docker start"
    exit 1
fi

rm -rf "$BUILD_DIR" "$ZIP_NAME"
mkdir -p "$BUILD_DIR"

echo "[1/3] Installing dependencies into build folder with Docker image $DOCKER_IMAGE..."
docker run --rm \
    --user "$(id -u):$(id -g)" \
    -e HOME=/tmp \
    -e PIP_NO_CACHE_DIR=1 \
    --entrypoint /bin/sh \
    -v "$PWD":/var/task \
    -w /var/task \
    "$DOCKER_IMAGE" \
    -c "python -m pip install --upgrade pip && pip install -r requirements.txt -t '$BUILD_DIR'"

echo "[2/3] Copying Lambda source files..."
cp lambda_function.py "$BUILD_DIR"/

if ! find "$BUILD_DIR/PIL" -maxdepth 1 -name '_imaging*.so' | grep -q .; then
    echo "Error: Pillow native module was not built correctly (missing PIL/_imaging*.so)."
    exit 1
fi

echo "[3/3] Building deployment zip..."
(
    cd "$BUILD_DIR"
    zip -qr "../$ZIP_NAME" .
)

echo "Successfully created $ZIP_NAME"

echo ""
read -p "Step 2: Upload $ZIP_NAME to AWS? (y/n): " confirm_upload
if [[ "$confirm_upload" =~ ^[Yy]$ ]]; then
    FILESIZE=$(stat -c%s "$ZIP_NAME")
    if [ "$FILESIZE" -gt 50000000 ]; then
        echo "File size ($FILESIZE bytes) is large (>50MB). Using S3 bridge (bucket: $S3_TEMP_BUCKET)..."
        aws s3 cp "$ZIP_NAME" "s3://$S3_TEMP_BUCKET/$ZIP_NAME"
        aws lambda update-function-code --function-name "$DIR_NAME" \
            --s3-bucket "$S3_TEMP_BUCKET" --s3-key "$ZIP_NAME" --region "$REGION"
        aws s3 rm "s3://$S3_TEMP_BUCKET/$ZIP_NAME"
    else
        aws lambda update-function-code --function-name "$DIR_NAME" \
            --zip-file "fileb://$ZIP_NAME" --region "$REGION"
    fi
else
    echo "Upload skipped."
    exit 0
fi

echo ""
read -p "Step 3: Create a new Lambda version? (y/n): " confirm_version
if [[ "$confirm_version" =~ ^[Yy]$ ]]; then
    read -p "Enter version description: " VERSION_DESC
    NEW_VERSION=$(aws lambda publish-version --function-name "$DIR_NAME" \
        --description "$VERSION_DESC" --query 'Version' --output text --region "$REGION")
    echo "Successfully created version: $NEW_VERSION"
else
    echo "Versioning skipped."
    exit 0
fi

echo ""
read -p "Step 4: Update aliases? (loc/dev/prod/all/n): " alias_choice
case "$alias_choice" in
    loc|dev|prod) TARGETS=("$alias_choice") ;;
    all) TARGETS=("loc" "dev" "prod") ;;
    *) echo "Alias update skipped."; exit 0 ;;
esac

for ALIAS in "${TARGETS[@]}"; do
    echo "Updating alias '$ALIAS' to version $NEW_VERSION..."
    aws lambda update-alias --function-name "$DIR_NAME" --name "$ALIAS" \
        --function-version "$NEW_VERSION" --region "$REGION" 2>/dev/null || \
    aws lambda create-alias --function-name "$DIR_NAME" --name "$ALIAS" \
        --function-version "$NEW_VERSION" --region "$REGION"
done

echo ">>> Deployment of $DIR_NAME completed successfully."


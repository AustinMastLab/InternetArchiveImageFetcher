# InternetArchiveImageFetcher

## Purpose
A Python Lambda that reliably downloads Internet Archive images using the official `internetarchive` library, then performs the same resize/upload/callback behavior as `BiospexImageFetcher`.

## Workflow
1. Triggered by SQS with the same message schema as `BiospexImageFetcher`.
2. Parses `accessURI` into Internet Archive `identifier` + IA file path.
3. Downloads the source image using `internetarchive.download(...)` to `/tmp`.
4. Validates format (`JPEG` or `PNG`), resizes with Pillow using "inside + no enlargement" behavior, outputs JPEG.
5. Uploads to S3 with matching metadata keys.
6. Sends status updates to `updatesQueueUrl`:
   - `export`: sends success and failure.
   - `ocr`: sends failure only (silent on success).

## Input Message (same as Node fetcher)
```json
{
  "taskType": "export",
  "accessURI": "https://ia801809.us.archive.org/4/items/.../image.jpg",
  "subjectId": "12345",
  "s3Bucket": "biospex-images",
  "s3Path": "incoming/12345.jpg",
  "updatesQueueUrl": "https://sqs.us-east-2.amazonaws.com/.../queue",
  "queueId": "12",
  "fileId": 34,
  "maxWidth": 1500,
  "maxHeight": 1500
}
```

## Environment Variables
- `JPEG_QUALITY` (default `80`)
- `DEFAULT_MAX_WIDTH` (default `1500`)
- `DEFAULT_MAX_HEIGHT` (default `1500`)
- `IA_RETRIES` (default `5`)
- `AWS_REGION` (default `us-east-2`)

## Handler
- Runtime: Python 3.12
- Handler: `lambda_function.lambda_handler`

## Dependencies
- `internetarchive`
- `Pillow`
- `boto3`

### Packaging Option A (bundle deps with function via Docker)
Use `deploy.sh` from this folder.

This Lambda includes Pillow, so dependencies should be built in a Lambda-compatible Python 3.12 environment. `deploy.sh` now uses the AWS Lambda base image `public.ecr.aws/lambda/python:3.12` to build the zip.

#### On-demand Docker workflow

Start Docker only when you need a build:

```bash
sudo service docker start
cd /data/web/InternetArchiveImageFetcher
./deploy.sh
sudo service docker stop
```

The build output is written locally to `function.zip`. Docker is only used as a temporary build environment.

#### Manual build equivalent

```bash
sudo service docker start
cd /data/web/InternetArchiveImageFetcher
rm -rf build function.zip

docker run --rm \
  --user "$(id -u):$(id -g)" \
  -e HOME=/tmp \
  -e PIP_NO_CACHE_DIR=1 \
  --entrypoint /bin/sh \
  -v "$PWD":/var/task \
  -w /var/task \
  public.ecr.aws/lambda/python:3.12 \
  -c "python -m pip install --upgrade pip && pip install -r requirements.txt -t build"

cp lambda_function.py build/
(cd build && zip -qr ../function.zip .)

sudo service docker stop
```

### Packaging Option B (Lambda Layer)
If you prefer a layer for faster code-only deploys:

```bash
mkdir -p layer/python
pip install -r requirements.txt -t layer/python
cd layer && zip -r internetarchive-layer.zip python
```

Then attach the layer to `InternetArchiveImageFetcher` and package only `lambda_function.py` in the function zip.

## Notes for SQS Trigger
- Trigger source: SQS queue used for IA-download jobs.
- Batch size: start with `1-5` to limit bursts to archive.org and reduce retries.
- Enable partial batch response only if you later add per-record batch item failure reporting.

## Operational Best Practices
- Set Lambda timeout to at least `60-120s` (IA requests can be slower than direct CDN URLs).
- Keep memory at `1024 MB` or higher for faster Pillow image decode/encode.
- Use reserved concurrency to protect archive.org and your downstream SQS/S3 pipeline.
- Always clean `/tmp` after each record; this handler already removes temp folders.
- Monitor throttling/retry patterns in CloudWatch and tune `IA_RETRIES` accordingly.


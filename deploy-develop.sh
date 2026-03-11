#!/bin/bash

###############################################################################
# Deploy BNI Conversation Service to Google Cloud Run - Development Environment
# Project: LAD-Develop
# Usage: ./deploy-develop.sh
###############################################################################

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
PROJECT_ID="lad-develop"
SERVICE_NAME="bni-conversation-service-develop"
REGION="us-central1"
IMAGE_NAME="gcr.io/${PROJECT_ID}/bni-conversation-service"

echo -e "${BLUE}╔════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║ BNI Conversation Service Deployment - Development     ║${NC}"
echo -e "${BLUE}╚════════════════════════════════════════════════════════╝${NC}"
echo ""

# Step 1: Verify gcloud is installed
echo -e "${YELLOW}[1/6]${NC} Checking gcloud installation..."
if ! command -v gcloud &> /dev/null; then
    echo -e "${RED}✗ Error: gcloud CLI is not installed${NC}"
    echo "Please install: https://cloud.google.com/sdk/docs/install"
    exit 1
fi
echo -e "${GREEN}✓ gcloud CLI found${NC}"
echo ""

# Step 2: Set active project
echo -e "${YELLOW}[2/6]${NC} Setting active GCP project to ${PROJECT_ID}..."
gcloud config set project ${PROJECT_ID}
echo -e "${GREEN}✓ Project set${NC}"
echo ""

# Step 3: Enable required APIs
echo -e "${YELLOW}[3/6]${NC} Ensuring required APIs are enabled..."
gcloud services enable \
    cloudbuild.googleapis.com \
    run.googleapis.com \
    containerregistry.googleapis.com \
    secretmanager.googleapis.com \
    --project=${PROJECT_ID}
echo -e "${GREEN}✓ APIs enabled${NC}"
echo ""

# Step 4: Build Docker image for AMD64 (Cloud Run architecture)
echo -e "${YELLOW}[4/6]${NC} Building Docker image for linux/amd64..."
echo -e "Image: ${IMAGE_NAME}:latest"
docker buildx build --platform linux/amd64 -t ${IMAGE_NAME}:latest --push .
echo -e "${GREEN}✓ Docker image built and pushed${NC}"
echo ""

# Step 5: Deploy to Cloud Run
echo -e "${YELLOW}[5/6]${NC} Deploying to Cloud Run..."
gcloud run deploy ${SERVICE_NAME} \
    --image=${IMAGE_NAME}:latest \
    --platform=managed \
    --region=${REGION} \
    --allow-unauthenticated \
    --port=8080 \
    --memory=512Mi \
    --cpu=1 \
    --min-instances=0 \
    --max-instances=10 \
    --concurrency=50 \
    --timeout=300 \
    --cpu-boost \
    --set-env-vars="ENVIRONMENT=development" \
    --project=${PROJECT_ID}
echo -e "${GREEN}✓ Deployment complete${NC}"
echo ""

# Step 6: Fetch service URL
echo -e "${YELLOW}[6/6]${NC} Fetching service URL..."
SERVICE_URL=$(gcloud run services describe ${SERVICE_NAME} --region=${REGION} --project=${PROJECT_ID} --format='value(status.url)')

echo -e "${BLUE}╔════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║          Deployment Successful! 🎉                    ║${NC}"
echo -e "${BLUE}╚════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "Service URL: ${GREEN}${SERVICE_URL}${NC}"
echo -e "Health Check: ${GREEN}${SERVICE_URL}/health${NC}"
echo ""

# Test health endpoint
echo "Testing health endpoint..."
sleep 2  # Give service a moment to start
if curl -s "${SERVICE_URL}/health" > /dev/null 2>&1; then
    echo -e "${GREEN}✓ Health check passed${NC}"
else
    echo -e "${YELLOW}⚠ Health check pending (service may be cold-starting)${NC}"
fi

curl -s "${SERVICE_URL}/health" | python -m json.tool 2>/dev/null || echo "Service initializing..."
echo ""
echo -e "${GREEN}✓ Deployment complete!${NC}"

# URI Social Backend - Deployment Guide

## Overview
This document describes the CI/CD deployment setup for the URI Social Backend (FastAPI service).

## Architecture

### Service Details
- **Framework**: FastAPI (Python 3.13)
- **Service Name**: `uri-agent.api`
- **Port**: 9003 (host) → 80 (container)
- **Network**: `uritest-network` (dev) / `uri-network` (prod)

### Docker Images
- **Development**: `uriteam/urisvc:uriagentdev`
- **Production**: `uriteam/urisvc:uriagentprod`

## Deployment Workflows

### Development (Staging)
**Branch**: `develop`
**Workflow**: `.github/workflows/uri-social-backend-docker-dev.yml`
**Trigger**: Push to `develop` branch or manual dispatch

**Steps**:
1. Checkout code
2. Set up Python 3.13
3. Install dependencies from `requirements.txt`
4. Build Docker image: `uriteam/urisvc:uriagentdev`
5. Push image to Docker Hub
6. Copy source files to staging VM (`~/uri-social-backend/`)
7. SSH into VM and rebuild image from source
8. Deploy using `docker-compose.dev.yml`
9. Cleanup old images

**Deployment Path**: `~/uri-social-backend/`
**VM User**: `uridev`

### Production
**Branch**: `master`
**Workflow**: `.github/workflows/uri-social-backend-docker-prod.yml`
**Trigger**: Push to `master` branch or manual dispatch

**Steps**: Same as development but uses:
- Image tag: `uriagentprod`
- VM user: `uriprod`
- docker-compose: `docker-compose.prod.yml`
- Network: `uri-network`

## Required GitHub Secrets

The following secrets must be configured in the GitHub repository settings:

### Development
- `DOCKER_USERNAME` - Docker Hub username
- `DOCKER_PASSWORD` - Docker Hub password
- `VM_IP_DEV` - Staging VM IP address
- `SSH_PRIVATE_KEY_DEV` - SSH private key for staging VM

### Production
- `DOCKER_USERNAME` - Docker Hub username (same as dev)
- `DOCKER_PASSWORD` - Docker Hub password (same as dev)
- `VM_IP` - Production VM IP address
- `SSH_PRIVATE_KEY` - SSH private key for production VM

## Environment Variables

Copy `.env.example` to `.env` on the VM and configure:

### Required Variables
```bash
# MongoDB
MONGODB_URI=mongodb://localhost:27017/uri_db
MONGODB_DB=uri_db

# OpenAI
OPENAI_API_KEY=sk-...

# JWT
AUTHJWT_SECRET_KEY=your_secret_key

# URI Microservices
URI_GATEWAY_BASE_API_URL=http://uri-gateway.api
URI_BACKEND_BASE_URL=http://uri-backend.api
URI_CLIENT_ID=your_client_id
URI_CLIENT_SECRET=your_client_secret

# Social platforms
META_API_KEY=
META_APP_ID=
META_APP_SECRET=
META_SYSTEM_TOKEN=

# imgBB (image hosting)
IMGBB_API_KEY=

# Outstand (social publishing)
OUTSTAND_API_KEY=

# Frontend URL
WEB_APP_URL=https://app.uricreative.com

# Environment
ENV=Development  # or Production
```

## Manual Deployment

### Prerequisites
1. Ensure Docker and docker-compose are installed on the VM
2. Ensure the `uritest-network` (dev) or `uri-network` (prod) exists:
   ```bash
   docker network create uritest-network  # dev
   docker network create uri-network      # prod
   ```

### Deploy to Development
```bash
# On your local machine
git push origin develop

# Or manually on the VM
cd ~/uri-social-backend
git pull origin develop
docker build -t uriteam/urisvc:uriagentdev .
docker-compose -f docker-compose.dev.yml down
docker-compose -f docker-compose.dev.yml up -d
```

### Deploy to Production
```bash
# On your local machine
git push origin master

# Or manually on the VM
cd ~/uri-social-backend
git pull origin master
docker build -t uriteam/urisvc:uriagentprod .
docker-compose -f docker-compose.prod.yml down
docker-compose -f docker-compose.prod.yml up -d
```

## Monitoring

### Check Container Status
```bash
docker ps | grep uri-agent
```

### View Logs
```bash
# Follow logs
docker logs -f uri-agent.api

# Last 100 lines
docker logs --tail 100 uri-agent.api
```

### Check Service Health
```bash
# Development
curl http://localhost:9003/

# Production
curl http://localhost:9003/
```

## Troubleshooting

### Container Won't Start
```bash
# Check logs
docker logs uri-agent.api

# Check environment variables
docker exec uri-agent.api env

# Verify network exists
docker network ls | grep uritest-network
```

### Port Already in Use
```bash
# Check what's using port 9003
sudo lsof -i :9003

# Stop conflicting container
docker stop <container_name>
```

### Image Pull Failures
```bash
# Re-login to Docker Hub
docker login

# Manually pull image
docker pull uriteam/urisvc:uriagentdev
```

## Rollback

To rollback to a previous version:
```bash
cd ~/uri-social-backend
git checkout <previous_commit_hash>
docker build -t uriteam/urisvc:uriagentdev .
docker-compose -f docker-compose.dev.yml down
docker-compose -f docker-compose.dev.yml up -d
```

## Next Steps

1. **Commit the workflow files**:
   ```bash
   git add .github/workflows/ docker-compose.prod.yml
   git commit -m "Add CI/CD workflows for develop and master branches"
   git push origin develop
   ```

2. **Verify GitHub Secrets**: Ensure all required secrets are configured in GitHub repository settings

3. **First Deployment**: Push to develop branch to trigger the first automated deployment

4. **Monitor**: Check GitHub Actions tab for workflow execution status

## Support

For issues or questions, contact the DevOps team.

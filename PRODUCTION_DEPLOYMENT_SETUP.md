# Production Deployment Setup Guide

Automated deployment from `main` branch to Production VM (20.9.131.143)

---

## 🔧 GitHub Secrets Configuration

You need to configure these secrets in your GitHub repository for the production deployment workflow to work.

### How to Add Secrets

1. Go to your GitHub repository: `https://github.com/urifusion-boop/uri-social-backend`
2. Navigate to **Settings** → **Secrets and variables** → **Actions**
3. Click **New repository secret**
4. Add each secret below

---

## 🔑 Required Secrets

### 1. **PRODUCTION_VM_IP**
- **Value:** `20.9.131.143`
- **Description:** IP address of the new production VM
- **Usage:** SSH connection and SCP file transfer

### 2. **PRODUCTION_VM_USERNAME**
- **Value:** `urisocialprod`
- **Description:** Username for SSH access to production VM
- **Usage:** SSH login and file ownership

### 3. **PRODUCTION_SSH_KEY**
- **Value:** Contents of `~/.ssh/urisocialprod_key.pem`
- **Description:** Private SSH key for production VM access
- **Usage:** Passwordless SSH authentication

**How to get the key:**
```bash
# On your local machine
cat ~/.ssh/urisocialprod_key.pem
```

**Important:** Copy the ENTIRE key including:
```
-----BEGIN RSA PRIVATE KEY-----
... all the key content ...
-----END RSA PRIVATE KEY-----
```

### 4. **DOCKER_USERNAME** (Should already exist)
- **Value:** Your Docker Hub username
- **Description:** Docker Hub account for pushing/pulling images
- **Usage:** Docker login and image operations
- **Note:** This should already be configured for existing workflows

### 5. **DOCKER_PASSWORD** (Should already exist)
- **Value:** Your Docker Hub password or access token
- **Description:** Docker Hub authentication
- **Usage:** Docker login
- **Note:** This should already be configured for existing workflows

---

## 📋 Quick Setup Checklist

- [ ] 1. Add `PRODUCTION_VM_IP` secret → `20.9.131.143`
- [ ] 2. Add `PRODUCTION_VM_USERNAME` secret → `urisocialprod`
- [ ] 3. Add `PRODUCTION_SSH_KEY` secret → Contents of `~/.ssh/urisocialprod_key.pem`
- [ ] 4. Verify `DOCKER_USERNAME` exists (should already be there)
- [ ] 5. Verify `DOCKER_PASSWORD` exists (should already be there)
- [ ] 6. Commit and push the new workflow file
- [ ] 7. Push `main` branch to trigger first deployment

---

## 🚀 Deployment Workflow

### Automatic Deployment
Every push to the `main` branch will automatically:

1. ✅ Build Docker image
2. ✅ Push to Docker Hub as `urisocial/backend:production`
3. ✅ Copy files to production VM
4. ✅ Pull latest image on production VM
5. ✅ Stop old container
6. ✅ Start new container
7. ✅ Verify container is running
8. ✅ Clean up old images

### Manual Deployment
You can also trigger deployment manually:

1. Go to GitHub repository
2. Navigate to **Actions** tab
3. Select **Deploy to Production VM (Main Branch)** workflow
4. Click **Run workflow**
5. Select `main` branch
6. Click **Run workflow**

---

## 📁 Files Deployed to Production VM

The workflow copies these files to `/home/urisocialprod/uri-social-backend/`:

- `app/` - Application code
- `docker-compose.prod.yml` - Production Docker Compose configuration
- `.env.production` - Production environment variables (if exists)
- `.env.example` - Environment template
- `Dockerfile` - Docker build instructions
- `requirements.txt` - Python dependencies

---

## 🔍 Verify Deployment

After deployment, the workflow will:

1. Check if container `urisocial-backend-prod` is running
2. Show container status and ports
3. Display recent logs (last 20 lines)
4. Exit with error if container fails to start

### Manual Verification

SSH into production VM and check:

```bash
# SSH into production VM
ssh -i ~/.ssh/urisocialprod_key.pem urisocialprod@20.9.131.143

# Check container status
docker ps | grep urisocial-backend-prod

# Check logs
docker logs urisocial-backend-prod --tail 50

# Check if API is responding
curl http://localhost/social-media/health  # Adjust endpoint as needed
```

---

## 🔄 Deployment Flow

```
Developer pushes to main branch
         ↓
GitHub Actions triggered
         ↓
Build Docker image
         ↓
Push to Docker Hub (urisocial/backend:production)
         ↓
Copy files to Production VM (20.9.131.143)
         ↓
SSH into Production VM
         ↓
Pull latest image
         ↓
Stop old container (docker compose down)
         ↓
Start new container (docker compose up -d)
         ↓
Verify container is running
         ↓
Show logs and status
         ↓
✅ Deployment complete!
```

---

## 📦 Docker Image Tags

The workflow creates two image tags:

1. **`urisocial/backend:production`** - Production stable version
2. **`urisocial/backend:latest`** - Latest build (always current)

The production VM uses: `urisocial/backend:production`

---

## ⚠️ Important Notes

### 1. **Environment Variables**
The `.env.production` file on the VM is NOT overwritten by the workflow to preserve secrets. Make sure it's properly configured on the VM:

```bash
ssh -i ~/.ssh/urisocialprod_key.pem urisocialprod@20.9.131.143
cd ~/uri-social-backend
cat .env.production  # Verify it exists and has correct values
```

### 2. **First Deployment**
Before the first deployment, ensure:

- [ ] `.env.production` exists on the VM
- [ ] Docker network `urisocial-production-network` exists
- [ ] Nginx is properly configured (with SSL certificates)

### 3. **Rollback**
If deployment fails, the old container remains stopped. To rollback:

```bash
# SSH into VM
ssh -i ~/.ssh/urisocialprod_key.pem urisocialprod@20.9.131.143

# Pull previous image version
docker pull urisocial/backend:production@sha256:<previous-digest>

# Restart with old image
cd ~/uri-social-backend
docker compose -f docker-compose.prod.yml up -d
```

### 4. **Zero Downtime**
Currently, there's a brief downtime during deployment (container stop → start). For true zero-downtime:

- Use blue-green deployment
- Or use rolling updates with multiple containers
- This can be added later if needed

---

## 🐛 Troubleshooting

### Workflow Fails at "Build and push Docker image"
**Issue:** Docker build or push failed

**Check:**
- Docker Hub credentials (`DOCKER_USERNAME` and `DOCKER_PASSWORD`)
- Dockerfile syntax
- Build logs in GitHub Actions

### Workflow Fails at "Copy files to Production VM"
**Issue:** SCP transfer failed

**Check:**
- `PRODUCTION_VM_IP` is correct: `20.9.131.143`
- `PRODUCTION_VM_USERNAME` is correct: `urisocialprod`
- `PRODUCTION_SSH_KEY` has complete key content (including header/footer)
- VM is accessible from GitHub Actions (firewall rules)

### Workflow Fails at "Deploy on Production VM"
**Issue:** Container fails to start

**Check:**
- `.env.production` exists on VM
- Docker network exists: `docker network ls | grep urisocial-production`
- Check container logs: `docker logs urisocial-backend-prod`
- Check docker-compose.prod.yml syntax

### Container Starts but Exits Immediately
**Issue:** Application crashes on startup

**Check:**
- Environment variables in `.env.production`
- MongoDB connection string
- Redis connection
- Application logs: `docker logs urisocial-backend-prod`

---

## 🔐 Security Best Practices

### GitHub Secrets
- ✅ Never commit secrets to repository
- ✅ Use GitHub Secrets for all sensitive data
- ✅ Rotate SSH keys periodically
- ✅ Use Docker Hub access tokens (not passwords)

### SSH Keys
- ✅ Private key stored only in GitHub Secrets
- ✅ Never expose private key in logs
- ✅ Use separate keys for dev/prod environments

### Environment Variables
- ✅ Keep `.env.production` only on VM (never in Git)
- ✅ Use strong passwords for databases
- ✅ Rotate API keys regularly

---

## 📊 Monitoring Deployment

### GitHub Actions
- View workflow runs: `Actions` tab in GitHub
- Check logs for each step
- See deployment history

### Production VM
```bash
# Check container status
docker ps

# Monitor logs in real-time
docker logs -f urisocial-backend-prod

# Check resource usage
docker stats urisocial-backend-prod

# Check container health
docker inspect urisocial-backend-prod --format='{{.State.Health.Status}}'
```

---

## 🎯 Next Steps After Setup

1. **Configure GitHub Secrets** (5 minutes)
   - Add all required secrets to GitHub repository

2. **Test Deployment** (10 minutes)
   - Make a small change to main branch
   - Push and verify workflow runs successfully
   - Check container is running on production VM

3. **Setup Monitoring** (Optional)
   - Configure log aggregation
   - Setup uptime monitoring
   - Add Slack/Discord notifications to workflow

4. **Fix SSL Certificates** (CRITICAL)
   - Follow [VM_MIGRATION_ANALYSIS.md](../VM_MIGRATION_ANALYSIS.md) to fix SSL
   - Ensure nginx container runs properly

5. **DNS Cutover** (When ready)
   - Update DNS A record
   - Point api.urisocial.com to 20.9.131.143
   - Monitor traffic

---

## 📞 Support

**Workflow Issues:**
- Check GitHub Actions logs
- Verify all secrets are configured
- Ensure VM is accessible

**Deployment Issues:**
- SSH into VM and check logs
- Verify container status
- Check environment configuration

**VM Configuration:**
- See [VM_MIGRATION_ANALYSIS.md](../VM_MIGRATION_ANALYSIS.md)
- Ensure SSL certificates are present
- Verify docker network exists

---

## ✅ Summary

**What's Configured:**
- ✅ New workflow: `.github/workflows/deploy-production.yml`
- ✅ Triggers on push to `main` branch
- ✅ Deploys to: 20.9.131.143 (urisocialprod)
- ✅ Container: `urisocial-backend-prod`
- ✅ Image: `urisocial/backend:production`

**What You Need to Do:**
1. Add 3 new GitHub secrets (PRODUCTION_VM_IP, PRODUCTION_VM_USERNAME, PRODUCTION_SSH_KEY)
2. Verify 2 existing secrets (DOCKER_USERNAME, DOCKER_PASSWORD)
3. Commit and push the workflow file
4. Push to `main` branch to trigger first deployment

**Estimated Setup Time:** 10-15 minutes

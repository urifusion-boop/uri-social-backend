#!/bin/bash

# ============================================
# URI Social SDK Backend - Quick Start Script
# ============================================
# This script helps you quickly set up and test the SDK authentication system

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Helper functions
print_header() {
    echo -e "\n${BLUE}========================================${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${BLUE}========================================${NC}\n"
}

print_success() {
    echo -e "${GREEN}✅ $1${NC}"
}

print_error() {
    echo -e "${RED}❌ $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠️  $1${NC}"
}

print_info() {
    echo -e "${BLUE}ℹ️  $1${NC}"
}

# Check if .env exists
check_env_file() {
    print_header "Step 1: Checking Environment Configuration"

    if [ ! -f .env ]; then
        print_error ".env file not found!"
        print_info "Creating .env from template..."

        if [ -f ENV_TEMPLATE.txt ]; then
            cp ENV_TEMPLATE.txt .env
            print_success "Created .env file"
            print_warning "Please edit .env file with your values before continuing"
            echo ""
            echo "Required variables:"
            echo "  - MONGODB_URL"
            echo "  - DATABASE_NAME"
            echo "  - JWT_SECRET"
            echo "  - CRON_SECRET (generate with: openssl rand -hex 32)"
            echo "  - ENVIRONMENT"
            echo "  - CORS_ALLOWED_ORIGINS"
            echo ""
            read -p "Press Enter after you've configured .env..."
        else
            print_error "ENV_TEMPLATE.txt not found!"
            exit 1
        fi
    else
        print_success ".env file exists"
    fi

    # Check for required variables
    source .env

    missing_vars=()

    [ -z "$MONGODB_URL" ] && missing_vars+=("MONGODB_URL")
    [ -z "$DATABASE_NAME" ] && missing_vars+=("DATABASE_NAME")
    [ -z "$JWT_SECRET" ] && missing_vars+=("JWT_SECRET")
    [ -z "$CRON_SECRET" ] && missing_vars+=("CRON_SECRET")

    if [ ${#missing_vars[@]} -gt 0 ]; then
        print_error "Missing required environment variables:"
        for var in "${missing_vars[@]}"; do
            echo "  - $var"
        done
        exit 1
    fi

    print_success "All required environment variables are set"
}

# Check Python dependencies
check_dependencies() {
    print_header "Step 2: Checking Dependencies"

    # Check if Python is installed
    if ! command -v python3 &> /dev/null; then
        print_error "Python 3 is not installed"
        exit 1
    fi
    print_success "Python 3 is installed"

    # Check if required packages are installed
    print_info "Checking required Python packages..."

    packages=("motor" "fastapi" "pydantic")
    missing_packages=()

    for package in "${packages[@]}"; do
        if ! python3 -c "import $package" 2> /dev/null; then
            missing_packages+=("$package")
        fi
    done

    if [ ${#missing_packages[@]} -gt 0 ]; then
        print_warning "Missing Python packages:"
        for pkg in "${missing_packages[@]}"; do
            echo "  - $pkg"
        done
        echo ""
        read -p "Install missing packages? (y/n) " -n 1 -r
        echo ""
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            pip install -r requirements.txt
            print_success "Packages installed"
        else
            print_error "Cannot proceed without required packages"
            exit 1
        fi
    else
        print_success "All required packages are installed"
    fi
}

# Run database setup
run_database_setup() {
    print_header "Step 3: Database Setup"

    print_info "Running database setup script..."
    echo ""

    python3 -m app.scripts.setup_api_key_system

    if [ $? -eq 0 ]; then
        print_success "Database setup completed"
    else
        print_error "Database setup failed"
        exit 1
    fi
}

# Check if SDK routers are integrated
check_main_py() {
    print_header "Step 4: Checking main.py Integration"

    if [ ! -f app/main.py ]; then
        print_error "app/main.py not found!"
        exit 1
    fi

    # Check if SDK router is imported
    if grep -q "from app.agents.social_media_manager.routers.sdk_router import router as sdk_router" app/main.py; then
        print_success "SDK router is imported in main.py"
    else
        print_warning "SDK router is NOT imported in main.py"
        print_info "You need to add these imports to app/main.py:"
        echo ""
        echo "from app.agents.social_media_manager.routers.sdk_router import router as sdk_router"
        echo "from app.agents.social_media_manager.routers.api_key_management_router import router as api_key_mgmt_router"
        echo "from app.cron.reset_api_key_limits import cron_router"
        echo "from app.config.cors_config import configure_cors"
        echo ""
        echo "And add these lines:"
        echo ""
        echo "configure_cors(app)  # Before routers!"
        echo "app.include_router(sdk_router, tags=[\"SDK\"])"
        echo "app.include_router(api_key_mgmt_router, tags=[\"API Keys\"])"
        echo "app.include_router(cron_router, tags=[\"Cron Jobs\"])"
        echo ""
        print_warning "See INTEGRATION_GUIDE.md for complete instructions"
        echo ""
        read -p "Press Enter after you've updated main.py..."
    fi

    # Check if routers are included
    if grep -q "app.include_router(sdk_router" app/main.py; then
        print_success "SDK router is included in main.py"
    else
        print_warning "SDK router is NOT included in main.py"
        print_info "See note above and INTEGRATION_GUIDE.md"
    fi
}

# Start server (optional)
start_server() {
    print_header "Step 5: Starting Server (Optional)"

    echo ""
    read -p "Do you want to start the development server? (y/n) " -n 1 -r
    echo ""

    if [[ $REPLY =~ ^[Yy]$ ]]; then
        print_info "Starting server on http://localhost:8000"
        print_info "Press Ctrl+C to stop"
        echo ""

        uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
    else
        print_info "Skipping server start"
        print_info "To start manually, run:"
        echo "  uvicorn app.main:app --reload --host 0.0.0.0 --port 8000"
    fi
}

# Test endpoints
test_endpoints() {
    print_header "Step 6: Testing Endpoints (Optional)"

    echo ""
    read -p "Do you want to test the endpoints? (y/n) " -n 1 -r
    echo ""

    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        print_info "Skipping endpoint tests"
        return
    fi

    # Check if server is running
    print_info "Checking if server is running on http://localhost:8000..."

    if ! curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/docs | grep -q "200"; then
        print_error "Server is not running on http://localhost:8000"
        print_info "Please start the server first:"
        echo "  uvicorn app.main:app --reload --host 0.0.0.0 --port 8000"
        return
    fi

    print_success "Server is running"

    # Check if API docs are accessible
    print_info "Checking API documentation..."

    if curl -s http://localhost:8000/docs | grep -q "SDK"; then
        print_success "SDK endpoints are accessible at http://localhost:8000/docs"
    else
        print_warning "SDK endpoints may not be properly configured"
    fi

    # Offer to create test API key
    echo ""
    read -p "Do you want to create a test API key? (y/n) " -n 1 -r
    echo ""

    if [[ $REPLY =~ ^[Yy]$ ]]; then
        print_info "You'll need a JWT token to create an API key"
        print_info "Please login to your application and get a JWT token"
        echo ""
        read -p "Enter your JWT token: " JWT_TOKEN

        if [ -z "$JWT_TOKEN" ]; then
            print_warning "No JWT token provided, skipping API key creation"
        else
            print_info "Creating test API key..."

            response=$(curl -s -X POST http://localhost:8000/social-media/api-keys/create \
                -H "Authorization: Bearer $JWT_TOKEN" \
                -H "Content-Type: application/json" \
                -d '{
                    "name": "Test API Key",
                    "description": "Created by quick_start.sh",
                    "environment": "development"
                }')

            if echo "$response" | grep -q "api_key"; then
                print_success "API key created successfully!"
                echo ""
                echo "$response" | python3 -m json.tool
                echo ""
                print_warning "SAVE THIS API KEY! You won't see it again."

                # Extract API key from response
                API_KEY=$(echo "$response" | python3 -c "import sys, json; print(json.load(sys.stdin)['api_key'])" 2>/dev/null)

                if [ ! -z "$API_KEY" ]; then
                    echo ""
                    print_info "Testing API key..."

                    # Test billing endpoint
                    test_response=$(curl -s -w "\n%{http_code}" -H "X-API-Key: $API_KEY" \
                        http://localhost:8000/api/v1/billing/credits)

                    http_code=$(echo "$test_response" | tail -n1)
                    response_body=$(echo "$test_response" | head -n-1)

                    if [ "$http_code" = "200" ]; then
                        print_success "API key works! Response:"
                        echo "$response_body" | python3 -m json.tool
                    else
                        print_error "API key test failed (HTTP $http_code)"
                        echo "$response_body"
                    fi
                fi
            else
                print_error "Failed to create API key"
                echo "$response"
            fi
        fi
    fi
}

# Main execution
main() {
    clear

    echo -e "${GREEN}"
    echo "╔════════════════════════════════════════╗"
    echo "║   URI Social SDK Backend Quick Start   ║"
    echo "╔════════════════════════════════════════╗"
    echo -e "${NC}"

    check_env_file
    check_dependencies
    run_database_setup
    check_main_py

    print_header "Setup Complete!"

    print_success "Backend SDK system is ready!"
    echo ""
    echo "Next steps:"
    echo "  1. Start the server: uvicorn app.main:app --reload"
    echo "  2. Visit http://localhost:8000/docs"
    echo "  3. Create API keys via dashboard"
    echo "  4. Test SDK endpoints"
    echo ""
    echo "Documentation:"
    echo "  - Integration Guide: INTEGRATION_GUIDE.md"
    echo "  - Deployment Checklist: DEPLOYMENT_CHECKLIST.md"
    echo "  - Environment Template: ENV_TEMPLATE.txt"
    echo ""

    # Ask if user wants to continue with optional steps
    start_server
}

# Run main function
main

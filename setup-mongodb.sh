#!/bin/bash

# MongoDB + Application Startup Script
# This script sets up and verifies the entire Docker + MongoDB + Python environment

set -e

echo "╔════════════════════════════════════════════════════════════════╗"
echo "║     Pflegedienst Invoicer - MongoDB Docker Setup              ║"
echo "╚════════════════════════════════════════════════════════════════╝"
echo ""

# Colors for output
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# ============================================================================
# STEP 1: Check Prerequisites
# ============================================================================

echo -e "${BLUE}▶ Step 1: Checking prerequisites...${NC}"
echo ""

# Check if Docker is installed
if ! command -v docker &> /dev/null; then
    echo -e "${RED}✗ Docker not found${NC}"
    echo "  Please install Docker: https://docs.docker.com/get-docker/"
    exit 1
fi
echo -e "${GREEN}✓ Docker installed: $(docker --version)${NC}"

# Check if Docker Compose is installed
if ! command -v docker-compose &> /dev/null; then
    echo -e "${RED}✗ Docker Compose not found${NC}"
    echo "  Please install Docker Compose: https://docs.docker.com/compose/install/"
    exit 1
fi
echo -e "${GREEN}✓ Docker Compose installed: $(docker-compose --version)${NC}"

# Check if Python is installed
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}✗ Python 3 not found${NC}"
    exit 1
fi
PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
echo -e "${GREEN}✓ Python installed: $PYTHON_VERSION${NC}"

# Check if virtual environment exists
if [ ! -d ".venv" ]; then
    echo -e "${YELLOW}⚠ Virtual environment not found, creating...${NC}"
    python3 -m venv .venv
    echo -e "${GREEN}✓ Virtual environment created${NC}"
fi

echo ""

# ============================================================================
# STEP 2: Start MongoDB with Docker
# ============================================================================

echo -e "${BLUE}▶ Step 2: Starting MongoDB with Docker Compose...${NC}"
echo ""

# Check if containers are already running
RUNNING=$(docker-compose ps 2>/dev/null | grep "mongodb" | grep "Up" || true)

if [ -z "$RUNNING" ]; then
    echo "Starting MongoDB containers..."
    docker-compose up -d
    echo -e "${GREEN}✓ Docker containers started${NC}"
    
    # Wait for MongoDB to be healthy
    echo "Waiting for MongoDB to be ready..."
    for i in {1..30}; do
        if docker-compose exec -T mongodb mongosh --eval "db.adminCommand('ping')" &>/dev/null; then
            echo -e "${GREEN}✓ MongoDB is ready${NC}"
            break
        fi
        if [ $i -eq 30 ]; then
            echo -e "${RED}✗ MongoDB failed to start after 30 seconds${NC}"
            echo "  Check logs: docker-compose logs mongodb"
            exit 1
        fi
        echo -n "."
        sleep 1
    done
else
    echo -e "${GREEN}✓ MongoDB already running${NC}"
fi

echo ""

# ============================================================================
# STEP 3: Install Python Dependencies
# ============================================================================

echo -e "${BLUE}▶ Step 3: Installing Python dependencies...${NC}"
echo ""

# Activate virtual environment
source .venv/bin/activate

# Upgrade pip
python3 -m pip install --quiet --upgrade pip setuptools wheel

# Install requirements
echo "Installing packages from requirements.txt..."
pip install --quiet -r requirements.txt

echo -e "${GREEN}✓ Python dependencies installed${NC}"

echo ""

# ============================================================================
# STEP 4: Test MongoDB Connection
# ============================================================================

echo -e "${BLUE}▶ Step 4: Testing MongoDB connection...${NC}"
echo ""

python3 << 'EOF'
import sys
sys.path.insert(0, '.')

try:
    from app.db.mongodb_config import get_database, health_check, get_connection_info
    
    # Test connection
    if health_check():
        print("✓ MongoDB connection successful")
    else:
        print("✗ MongoDB health check failed")
        sys.exit(1)
    
    # Get connection info
    info = get_connection_info()
    print(f"✓ Database: {info['database']}")
    print(f"✓ Server version: {info['version']}")
    print(f"✓ Connection pool: Min={info['pool_size']['min']}, Max={info['pool_size']['max']}")
    
except ImportError as e:
    print(f"✗ Import error: {e}")
    print("  Make sure you're in the project directory and venv is activated")
    sys.exit(1)
except Exception as e:
    print(f"✗ Connection test failed: {e}")
    print("\nTroubleshooting steps:")
    print("1. Check MongoDB is running: docker-compose ps")
    print("2. Check MongoDB logs: docker-compose logs mongodb")
    print("3. Verify .env file has correct credentials")
    sys.exit(1)
EOF

if [ $? -ne 0 ]; then
    exit 1
fi

echo ""

# ============================================================================
# STEP 5: Initialize MongoDB Schema (create collections and indexes)
# ============================================================================

echo -e "${BLUE}▶ Step 5: Initializing MongoDB schema...${NC}"
echo ""

python3 << 'EOF'
import sys
sys.path.insert(0, '.')

try:
    from app.db.mongodb_config import get_database
    
    db = get_database()
    collections = db.list_collection_names()
    
    if collections:
        print(f"✓ Found {len(collections)} collections:")
        for coll in collections:
            count = db[coll].count_documents({})
            indexes = len(list(db[coll].list_indexes()))
            print(f"  - {coll}: {count} documents, {indexes} indexes")
    else:
        print("✗ No collections found. Schema initialization may have failed.")
        sys.exit(1)
        
except Exception as e:
    print(f"✗ Schema check failed: {e}")
    sys.exit(1)
EOF

if [ $? -ne 0 ]; then
    exit 1
fi

echo ""

# ============================================================================
# STEP 6: Display Summary and Next Steps
# ============================================================================

echo -e "${GREEN}╔════════════════════════════════════════════════════════════════╗"
echo "║                    ✓ SETUP COMPLETE                            ║"
echo "╚════════════════════════════════════════════════════════════════╝${NC}"
echo ""

echo -e "${BLUE}MongoDB Status:${NC}"
docker-compose ps
echo ""

echo -e "${BLUE}Access Points:${NC}"
echo "  MongoDB URI: mongodb://pflegedienst_user:pflegedienst_password@localhost:27017"
echo "  MongoDB Admin UI: http://localhost:8081"
echo "  Admin username: admin"
echo "  Admin password: admin"
echo ""

echo -e "${BLUE}Useful Commands:${NC}"
echo "  Show MongoDB logs:       docker-compose logs -f mongodb"
echo "  Connect to MongoDB CLI:  docker-compose exec mongodb mongosh"
echo "  Stop containers:         docker-compose down"
echo "  View MongoDB data:       Open http://localhost:8081 in browser"
echo "  Restart containers:      docker-compose restart"
echo ""

echo -e "${BLUE}Next Steps:${NC}"
echo "  1. Start the Python backend:"
echo "     source .venv/bin/activate"
echo "     python backend.py"
echo ""
echo "  2. Monitor MongoDB:"
echo "     docker-compose logs -f mongodb"
echo ""
echo "  3. Create migration script:"
echo "     See MONGODB_MIGRATION_PLAN.md Step 2.x"
echo ""

echo -e "${YELLOW}Important:${NC}"
echo "  - Keep Docker containers running while using the application"
echo "  - MongoDB data persists in Docker volumes (survives container restart)"
echo "  - Change MONGO_ROOT_PASSWORD in .env for production use"
echo ""

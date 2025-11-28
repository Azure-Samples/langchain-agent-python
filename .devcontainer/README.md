# Dev Container Configuration

This folder contains the development container configuration for GitHub Codespaces and VS Code Remote - Containers.

## What's Included

### Base Image
- **Python 3.11** (Debian Bullseye)
- Pre-configured with `vscode` user

### Tools & CLIs
- **Azure CLI** - Manage Azure resources
- **Azure Developer CLI (azd)** - Deploy with `azd up`
- **Docker & Docker Compose** - Run PostgreSQL locally
- **GitHub CLI** - Interact with GitHub
- **Git** - Version control

### VS Code Extensions
- Python language support (Pylance, debugpy)
- Azure Bicep language support
- Docker extension
- GitHub Copilot & Copilot Chat
- Jupyter notebooks
- YAML and JSON support

### Python Packages
All dependencies from:
- `agent/requirements.txt`
- `mcp/requirements.txt`
- Development tools: black, pylint, pytest

### Port Forwarding
- **8000** - MCP Server
- **8080** - Agent (optional)

## Usage

### GitHub Codespaces

1. Click "Code" → "Codespaces" → "Create codespace on main"
2. Wait for container to build and post-create script to run
3. Update `.env.local` with your Azure credentials
4. Run `azd auth login` to authenticate
5. Start developing!

### VS Code Remote - Containers

1. Install the "Dev Containers" extension
2. Open this repository in VS Code
3. Click "Reopen in Container" when prompted
4. Wait for setup to complete

## First-Time Setup

After the container starts:

```bash
# 1. Authenticate with Azure
azd auth login
az login

# 2. Update environment variables
# Edit .env.local with your Azure OpenAI credentials

# 3. Deploy to Azure (optional)
azd up

# 4. Or run locally
docker-compose up -d              # Start PostgreSQL
cd data && python generate_database.py  # Initialize database
cd mcp && python mcp_server.py    # Terminal 1
cd agent && python agent.py       # Terminal 2
```

## Configuration Files

- **devcontainer.json** - Main configuration
  - Base image and features
  - VS Code extensions and settings
  - Port forwarding
  - Post-create command

- **post-create.sh** - Runs after container creation
  - Installs Python dependencies
  - Sets up development tools
  - Creates .env.local from template
  - Displays next steps

## Customization

### Add VS Code Extensions

Edit `devcontainer.json`:

```json
"extensions": [
  "your.extension-id"
]
```

### Install Additional Tools

Edit `post-create.sh` to add more setup steps.

### Change Python Version

Update the base image in `devcontainer.json`:

```json
"image": "mcr.microsoft.com/devcontainers/python:1-3.12-bullseye"
```

## Troubleshooting

**Container won't start:**
- Check Docker Desktop is running
- Try rebuilding: "Dev Containers: Rebuild Container"

**Dependencies not installed:**
- Run manually: `bash .devcontainer/post-create.sh`

**Azure CLI login issues:**
- Use device code flow: `az login --use-device-code`
- For azd: `azd auth login --use-device-code`

**Ports not forwarding:**
- Check Ports panel in VS Code
- Manually forward: "Forward a Port" command

## References

- [Dev Containers Specification](https://containers.dev/)
- [VS Code Remote - Containers](https://code.visualstudio.com/docs/remote/containers)
- [GitHub Codespaces](https://docs.github.com/codespaces)

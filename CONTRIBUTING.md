# Contributing to LangChain Agent with MCP

Thank you for your interest in contributing! This document provides guidelines for contributing to this project.

## Ways to Contribute

- **Report bugs**: Open an issue describing the bug and how to reproduce it
- **Suggest features**: Open an issue describing the feature and why it would be useful
- **Submit pull requests**: Fix bugs, add features, or improve documentation
- **Improve documentation**: Help make the docs clearer and more comprehensive

## Development Setup

1. **Fork and clone the repository**:
   ```bash
   git clone https://github.com/YOUR_USERNAME/langchain-agent-python.git
   cd langchain-agent-python
   ```

2. **Install dependencies**:
   ```bash
   cd agent && pip install -r requirements.txt
   cd ../mcp && pip install -r requirements.txt
   ```

3. **Set up local environment**:
   ```bash
   cp .env.example .env.local
   # Edit .env.local with your Azure OpenAI details
   ```

4. **Test locally**:
   ```bash
   # Terminal 1
   cd mcp && python mcp_server.py
   
   # Terminal 2
   cd agent && python agent.py
   ```

## Code Style

- Follow [PEP 8](https://www.python.org/dev/peps/pep-0008/) for Python code
- Use type hints where applicable
- Add docstrings to all functions and classes
- Keep functions focused and modular

## Bicep Templates

- Follow [Azure Bicep best practices](https://learn.microsoft.com/azure/azure-resource-manager/bicep/best-practices)
- Use parameters for values that might change
- Add metadata descriptions to all resources
- Follow the existing naming conventions

## Testing Your Changes

1. **Local testing**: Ensure both agent and MCP server run without errors
2. **Azure deployment**: Test with `azd up` to verify infrastructure changes
3. **Documentation**: Update README.md if adding new features

## Submitting Pull Requests

1. Create a new branch for your changes:
   ```bash
   git checkout -b feature/my-new-feature
   ```

2. Make your changes and commit:
   ```bash
   git add .
   git commit -m "Add: description of your changes"
   ```

3. Push to your fork:
   ```bash
   git push origin feature/my-new-feature
   ```

4. Open a pull request on GitHub with:
   - Clear title describing the change
   - Description of what changed and why
   - Any related issue numbers

## Code Review Process

- Maintainers will review your PR and may request changes
- Address feedback by pushing new commits to your branch
- Once approved, a maintainer will merge your PR

## Contributor License Agreement

Most contributions require you to agree to a Contributor License Agreement (CLA) declaring that you have the right to grant us the rights to use your contribution. For details, visit https://cla.opensource.microsoft.com.

## Questions?

Open an issue or see [SUPPORT.md](SUPPORT.md) for help.

Thank you for contributing! 🎉

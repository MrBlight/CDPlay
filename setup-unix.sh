#!/bin/bash
# CDPlayer v1.16.1 - Unix/Linux/macOS/BSD Setup Script
# Adds cdplay command to PATH

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$HOME/.local/bin"
UNINSTALL=false

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --uninstall|-u)
            UNINSTALL=true
            shift
            ;;
        --help|-h)
            echo "Usage: setup-unix.sh [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  -u, --uninstall   Remove cdplay from PATH"
            echo "  -h, --help        Show this help message"
            echo ""
            echo "This script creates a 'cdplay' command that launches the appropriate"
            echo "CD Player version for your operating system."
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

echo "=============================================="
echo "  CDPlayer v1.16.1 - Unix Setup"
echo "=============================================="
echo ""

if [ "$UNINSTALL" = true ]; then
    echo "Removing cdplay from PATH..."
    
    # Remove symlink
    if [ -f "$INSTALL_DIR/cdplay" ]; then
        rm -f "$INSTALL_DIR/cdplay"
        echo "  ✓ Removed $INSTALL_DIR/cdplay"
    fi
    
    # Remove uncdplayer symlink
    if [ -f "$INSTALL_DIR/uncdplayer" ]; then
        rm -f "$INSTALL_DIR/uncdplayer"
        echo "  ✓ Removed $INSTALL_DIR/uncdplayer"
    fi
    
    # Try to remove from shell configs (best effort)
    for config in "$HOME/.bashrc" "$HOME/.zshrc" "$HOME/.bash_profile"; do
        if [ -f "$config" ]; then
            grep -q "$INSTALL_DIR/cdplay" "$config" 2>/dev/null && \
                sed -i '/CDPlayer setup/d' "$config" && \
                echo "  ✓ Cleaned $config" || true
        fi
    done
    
    echo ""
    echo "Uninstallation complete!"
    echo "Run 'hash -r' or restart your terminal to update PATH."
    exit 0
fi

# Detect OS
OS=$(uname -s)
case $OS in
    Linux)
        VERSION_FILE="LB-Ver.py"
        OS_NAME="Linux"
        ;;
    Darwin)
        VERSION_FILE="Mac-Ver.py"
        OS_NAME="macOS"
        ;;
    FreeBSD|OpenBSD|NetBSD|DragonFly)
        VERSION_FILE="BSD-Ver.py"
        OS_NAME="BSD"
        ;;
    *)
        echo "[ERROR] Unsupported operating system: $OS"
        echo "Please use LB-Ver.py (Linux), Mac-Ver.py (macOS), or BSD-Ver.py (BSD)"
        exit 1
        ;;
esac

echo "Detected: $OS_NAME"
echo "Using: $VERSION_FILE"
echo ""

# Check if Python3 is available
if ! command -v python3 &> /dev/null; then
    echo "[ERROR] python3 not found!"
    echo "Please install Python 3.9 or later."
    exit 1
fi

PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
echo "Python version: $PYTHON_VERSION"

# Check minimum Python version (3.9)
PY_MAJOR=$(echo $PYTHON_VERSION | cut -d. -f1)
PY_MINOR=$(echo $PYTHON_VERSION | cut -d. -f2)

if [ "$PY_MAJOR" -lt 3 ] || ([ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 9 ]); then
    echo "[WARNING] Python 3.9+ recommended. Current: $PYTHON_VERSION"
fi

# Create installation directory
if [ ! -d "$INSTALL_DIR" ]; then
    mkdir -p "$INSTALL_DIR"
    echo "Created $INSTALL_DIR"
fi

# Add to PATH if not already there
if [[ ":$PATH:" != *":$INSTALL_DIR:"* ]]; then
    echo ""
    echo "Adding $INSTALL_DIR to PATH..."
    
    # Add to .bashrc and .zshrc
    for config in "$HOME/.bashrc" "$HOME/.zshrc"; do
        if ! grep -q "$INSTALL_DIR" "$config" 2>/dev/null; then
            echo "" >> "$config"
            echo "# CDPlayer setup - added by setup-unix.sh" >> "$config"
            echo "export PATH=\"$INSTALL_DIR:\$PATH\"" >> "$config"
            echo "  ✓ Added to $config"
        fi
    done
    
    echo ""
    echo "Note: Run 'source ~/.bashrc' or 'source ~/.zshrc' to apply PATH changes."
fi

# Create wrapper script for cdplay
cat > "$INSTALL_DIR/cdplay" << 'WRAPPER'
#!/bin/bash
# CDPlayer wrapper - auto-detects OS and runs appropriate version

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CDPLAYER_DIR="$(dirname "$SCRIPT_DIR")"

# Find the CDPlayer directory (could be parent or same level)
if [ -f "$CDPLAYER_DIR/LB-Ver.py" ]; then
    DIR="$CDPLAYER_DIR"
elif [ -f "./LB-Ver.py" ]; then
    DIR="."
elif [ -f "../LB-Ver.py" ]; then
    DIR=".."
else
    # Search in common locations
    for d in "$HOME/cdplayer" "/opt/cdplayer" "/usr/local/share/cdplayer"; do
        if [ -f "$d/LB-Ver.py" ] || [ -f "$d/Win-Ver.py" ]; then
            DIR="$d"
            break
        fi
    done
fi

if [ -z "$DIR" ] || [ ! -d "$DIR" ]; then
    echo "[ERROR] Cannot find CDPlayer installation directory."
    echo "Please ensure LB-Ver.py, Mac-Ver.py, or Win-Ver.py is accessible."
    exit 1
fi

# Detect OS and run appropriate version
OS=$(uname -s)
case $OS in
    Linux)
        exec python3 "$DIR/LB-Ver.py" "$@"
        ;;
    Darwin)
        exec python3 "$DIR/Mac-Ver.py" "$@"
        ;;
    FreeBSD|OpenBSD|NetBSD|DragonFly)
        exec python3 "$DIR/BSD-Ver.py" "$@"
        ;;
    *)
        echo "[ERROR] Unsupported OS: $OS"
        echo "Manual options:"
        echo "  python3 $DIR/LB-Ver.py   (Linux)"
        echo "  python3 $DIR/Mac-Ver.py  (macOS)"
        echo "  python3 $DIR/BSD-Ver.py  (BSD)"
        exit 1
        ;;
esac
WRAPPER

chmod +x "$INSTALL_DIR/cdplay"
echo "✓ Created $INSTALL_DIR/cdplay"

# Create wrapper script for uncdplayer (uninstall)
cat > "$INSTALL_DIR/uncdplayer" << UNINSTALL_WRAPPER
#!/bin/bash
# CDPlayer uninstaller wrapper
exec bash "$SCRIPT_DIR/setup-unix.sh" --uninstall "\$@"
UNINSTALL_WRAPPER

chmod +x "$INSTALL_DIR/uncdplayer"
echo "✓ Created $INSTALL_DIR/uncdplayer"

echo ""
echo "=============================================="
echo "  Installation Complete!"
echo "=============================================="
echo ""
echo "You can now run CDPlayer from anywhere using:"
echo "  cdplay          # Launch CD Player"
echo "  uncdplayer      # Uninstall CDPlayer"
echo ""
echo "Or run directly:"
echo "  python3 $SCRIPT_DIR/$VERSION_FILE"
echo ""
echo "Debug modes:"
echo "  python3 $VERSION_FILE --linux-debug   (Linux debug bypass)"
echo "  python3 Mac-Ver.py --mac-debug        (macOS debug bypass)"
echo "  python3 BSD-Ver.py --bsd-debug        (BSD debug bypass)"
echo ""
echo "To apply PATH changes immediately:"
echo "  source ~/.bashrc   # or ~/.zshrc"
echo ""

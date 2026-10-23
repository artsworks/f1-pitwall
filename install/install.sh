#!/bin/sh

install_pitwall() {
    os=$(uname -s)
    case "$os" in
        Darwin|Linux) ;;
        *)
            printf '%s\n' "This installer supports macOS and Linux only."
            return 1
            ;;
    esac

    uv_installed=0
    if command -v uv >/dev/null 2>&1; then
        uv_path=$(command -v uv)
    elif [ -x "$HOME/.local/bin/uv" ]; then
        uv_path="$HOME/.local/bin/uv"
    else
        printf '%s\n' "Installing uv."
        curl -LsSf https://astral.sh/uv/install.sh </dev/null | sh
        if [ -x "$HOME/.local/bin/uv" ]; then
            uv_path="$HOME/.local/bin/uv"
        else
            uv_path=$(command -v uv 2>/dev/null || true)
        fi
        uv_installed=1
    fi

    if [ -f "$HOME/.local/bin/env" ]; then
        # shellcheck source=/dev/null
        . "$HOME/.local/bin/env"
    fi
    if [ -n "${XDG_BIN_HOME:-}" ]; then
        PATH="$XDG_BIN_HOME:$PATH"
    fi
    PATH="$HOME/.local/bin:$PATH"
    export PATH
    if [ "$uv_installed" -eq 1 ]; then
        printf '%s\n' "Open a new terminal to use uv yourself."
    fi
    if [ -z "$uv_path" ]; then
        uv_path=$(command -v uv 2>/dev/null || true)
    fi
    if [ -z "$uv_path" ]; then
        printf '%s\n' "uv was not found. Open a new terminal and run this installer again."
        return 1
    fi
    case "$uv_path" in
        /*) ;;
        *)
            uv_dir=$(CDPATH='' cd "$(dirname "$uv_path")" && pwd -P) || return 1
            uv_path="$uv_dir/$(basename "$uv_path")"
            ;;
    esac

    if ! command -v git >/dev/null 2>&1; then
        if [ "$os" = "Darwin" ]; then
            if ! xcode-select -p >/dev/null 2>&1; then
                xcode-select --install
                printf '%s\n' "Finish the Command Line Tools install window, then run this installer again."
                return 1
            fi
            printf '%s\n' "Git is missing. Install the Xcode Command Line Tools, then run this installer again."
            return 1
        fi

        if command -v apt-get >/dev/null 2>&1; then
            sudo apt-get install -y git
            git_install_status=$?
        elif command -v dnf >/dev/null 2>&1; then
            sudo dnf install -y git
            git_install_status=$?
        elif command -v pacman >/dev/null 2>&1; then
            sudo pacman -S --noconfirm git
            git_install_status=$?
        elif command -v zypper >/dev/null 2>&1; then
            sudo zypper install -y git
            git_install_status=$?
        else
            printf '%s\n' "Install Git with your package manager, then run this installer again."
            return 1
        fi
        if [ "$git_install_status" -ne 0 ]; then
            printf '%s\n' "Git installation failed."
            return 1
        fi
    fi
    if ! command -v git >/dev/null 2>&1; then
        printf '%s\n' "Git was not found. Open a new terminal and run this installer again."
        return 1
    fi

    install_dir=${PITWALL_DIR:-"$HOME/f1-pitwall"}
    case "$install_dir" in
        /*) ;;
        *) install_dir="$PWD/$install_dir" ;;
    esac
    install_parent=$(dirname "$install_dir")
    install_name=$(basename "$install_dir")
    mkdir -p "$install_parent" || {
        printf '%s\n' "Could not create the parent folder for PITWALL_DIR: $install_parent"
        return 1
    }
    install_parent=$(CDPATH='' cd "$install_parent" && pwd -P) || return 1
    install_dir="$install_parent/$install_name"

    if [ -e "$install_dir" ]; then
        top_level=$(git -C "$install_dir" rev-parse --show-toplevel </dev/null 2>/dev/null)
        git_status=$?
        if [ "$git_status" -ne 0 ] || [ ! -f "$install_dir/pyproject.toml" ]; then
            printf 'PITWALL_DIR exists and is not a Pitwall git clone: %s\n' "$install_dir"
            return 1
        fi
        top_level=$(CDPATH='' cd "$top_level" 2>/dev/null && pwd -P)
        if [ "$top_level" != "$install_dir" ]; then
            printf 'PITWALL_DIR exists and is not a Pitwall git clone: %s\n' "$install_dir"
            return 1
        fi

        changes=$(git -C "$install_dir" status --porcelain --untracked-files=no </dev/null)
        git_status=$?
        if [ "$git_status" -ne 0 ]; then
            printf '%s\n' "Could not check the Pitwall folder. Pitwall did not update."
            return 1
        fi
        if [ -n "$changes" ]; then
            printf '%s\n' "Tracked files have local changes. Skipping git pull; uv sync will still run."
        else
            if ! git -C "$install_dir" pull --ff-only </dev/null; then
                printf '%s\n' "git pull failed. Pitwall did not update."
                return 1
            fi
        fi
    else
        if ! git clone https://github.com/artsworks/f1-pitwall.git "$install_dir" </dev/null; then
            printf '%s\n' "Git could not download Pitwall."
            return 1
        fi
    fi

    if ! (CDPATH='' cd "$install_dir" && "$uv_path" sync </dev/null); then
        printf '%s\n' "uv sync failed. Pitwall did not finish installing."
        return 1
    fi

    printf '%s\n' "Where will you open the dashboard? 1) On the Windows game PC (second monitor)  2) On this computer (default 1)"
    dashboard_choice=
    { IFS= read -r dashboard_choice </dev/tty; } 2>/dev/null || dashboard_choice=
    case "$dashboard_choice" in
        ""|1) dashboard_choice=1 ;;
        2) ;;
        *)
            printf '%s\n' "Enter 1 or 2, then run this installer again."
            return 1
            ;;
    esac

    printf '%s' "Download the natural voice (about 60 MB)? [Y/n] "
    voice_answer=
    { IFS= read -r voice_answer </dev/tty; } 2>/dev/null || voice_answer=
    case "$voice_answer" in
        n|N) ;;
        *)
            if ! (CDPATH='' cd "$install_dir" && "$uv_path" run pitwall voice get </dev/null); then
                printf '%s\n' "The natural voice did not download. Run uv run pitwall voice get later."
            fi
            ;;
    esac

    lan_ip=
    if [ "$os" = "Linux" ]; then
        lan_ip=$(hostname -I 2>/dev/null | awk '{print $1}')
        if [ -z "$lan_ip" ] && command -v ip >/dev/null 2>&1; then
            route_output=$(ip route get 1.1.1.1 2>/dev/null)
            lan_ip=$(printf '%s\n' "$route_output" | sed -n 's/.* src \([^ ]*\).*/\1/p' | head -n 1)
        fi
    else
        lan_ip=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null)
    fi
    if [ -z "$lan_ip" ]; then
        lan_ip="the LAN address pitwall prints at start"
    fi

    quoted_dir=$(printf '%s' "$install_dir" | sed "s/'/'\\\\''/g")
    quoted_uv=$(printf '%s' "$uv_path" | sed "s/'/'\\\\''/g")
    if [ "$os" = "Darwin" ]; then
        desktop="$HOME/Desktop"
        mkdir -p "$desktop"
        cat > "$desktop/Pitwall.command" <<EOF
#!/bin/sh
cd '$quoted_dir' || exit 1
'$quoted_uv' run pitwall start
EOF
        cat > "$desktop/Pitwall Update.command" <<EOF
#!/bin/sh
cd '$quoted_dir' || exit 1
'$quoted_uv' run pitwall update
EOF
        chmod +x "$desktop/Pitwall.command" "$desktop/Pitwall Update.command"
    else
        data_home=${XDG_DATA_HOME:-"$HOME/.local/share"}
        launcher_dir="$data_home/pitwall"
        application_dir="$data_home/applications"
        mkdir -p "$launcher_dir" "$application_dir"
        start_script="$launcher_dir/start.sh"
        update_script="$launcher_dir/update.sh"
        cat > "$start_script" <<EOF
#!/bin/sh
cd '$quoted_dir' || exit 1
'$quoted_uv' run pitwall start
printf 'Press Enter to close. '
read -r _
EOF
        cat > "$update_script" <<EOF
#!/bin/sh
cd '$quoted_dir' || exit 1
'$quoted_uv' run pitwall update
printf 'Press Enter to close. '
read -r _
EOF
        chmod +x "$start_script" "$update_script"
        cat > "$application_dir/pitwall.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Pitwall
Exec="$start_script"
Terminal=true
Icon=$install_dir/docs/android-chrome-192x192.png
Categories=Game;
EOF
        cat > "$application_dir/pitwall-update.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Pitwall Update
Exec="$update_script"
Terminal=true
Icon=$install_dir/docs/android-chrome-192x192.png
Categories=Game;
EOF
        if command -v xdg-user-dir >/dev/null 2>&1; then
            desktop_dir=$(xdg-user-dir DESKTOP 2>/dev/null)
        else
            desktop_dir=
        fi
        if [ -z "$desktop_dir" ] || [ ! -d "$desktop_dir" ]; then
            if [ -d "$HOME/Desktop" ]; then
                desktop_dir="$HOME/Desktop"
            fi
        fi
        if [ -n "$desktop_dir" ] && [ -d "$desktop_dir" ]; then
            cp "$application_dir/pitwall.desktop" "$desktop_dir/"
            cp "$application_dir/pitwall-update.desktop" "$desktop_dir/"
            chmod +x "$desktop_dir/pitwall.desktop" "$desktop_dir/pitwall-update.desktop"
            if command -v gio >/dev/null 2>&1; then
                gio set "$desktop_dir/pitwall.desktop" metadata::trusted true 2>/dev/null || true
                gio set "$desktop_dir/pitwall-update.desktop" metadata::trusted true 2>/dev/null || true
            fi
        fi
    fi

(CDPATH='' cd "$install_dir" && "$uv_path" run pitwall doctor </dev/null) || {
    printf '%s\n' "Pitwall doctor returned an error."
}
printf '%s\n' "0 datagrams is normal until you are on track."

    printf '\n%s\n' "Pitwall is installed in $install_dir."
    printf '%s\n' "This computer plays no engineer audio. The voice plays in the browser tab that shows the dashboard. Open the dashboard on the game PC to hear it in your headset."
    printf '%s\n' "F1 26 Telemetry Settings: UDP Telemetry On, Broadcast Off, IP $lan_ip, Port 20777, Format 2026, Send Rate 30 Hz."
    if [ "$dashboard_choice" -eq 1 ]; then
        printf 'On the game PC, open http://%s:8000/?sound=1, click ENABLE SOUND, and enter the 4-digit PIN Pitwall prints at start. The PIN changes every start.\n' "$lan_ip"
    else
        printf '%s\n' "On this computer, open http://localhost:8000/?sound=1 and click ENABLE SOUND. No PIN is needed. The voice plays here."
    fi
    if [ "$os" = "Darwin" ]; then
        printf '%s\n' "If macOS asks whether python may accept incoming network connections, click Allow."
        printf '%s\n' "Guide: https://pitwall.arthurku.com/install.html#mac"
        printf '%s\n' "Launchers: ~/Desktop/Pitwall.command and ~/Desktop/Pitwall Update.command"
    else
        if command -v ufw >/dev/null 2>&1; then
            printf '%s\n' "If you use ufw, run: sudo ufw allow 20777/udp"
            printf '%s\n' "If you use ufw, run: sudo ufw allow 8000/tcp"
        elif command -v firewall-cmd >/dev/null 2>&1; then
            printf '%s\n' "If you use firewalld, run: sudo firewall-cmd --permanent --add-port=20777/udp --add-port=8000/tcp"
            printf '%s\n' "Then run: sudo firewall-cmd --reload"
        else
            printf '%s\n' "Allow UDP 20777 and TCP 8000 if you run a firewall."
        fi
        printf '%s\n' "Guide: https://pitwall.arthurku.com/install.html#linux"
        printf 'Launchers: %s/start.sh and %s/update.sh\n' "$launcher_dir" "$launcher_dir"
    fi
    printf '%s\n' "Start Pitwall from the Pitwall launcher. To update, use Pitwall Update."
}

install_pitwall

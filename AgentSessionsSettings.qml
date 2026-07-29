import QtQuick
import Quickshell
import qs.Common
import qs.Modules.Plugins
import qs.Widgets

PluginSettings {
    id: root
    pluginId: "agentSessions"

    StyledText {
        width: parent.width
        text: "Agent Sessions"
        font.pixelSize: Theme.fontSizeLarge
        font.weight: Font.Bold
        color: Theme.surfaceText
    }

    StringSetting {
        settingKey: "trigger"
        label: "Trigger Prefix"
        placeholder: "agent:"
        defaultValue: "agent:"
    }

    StringSetting {
        settingKey: "hosts"
        label: "Legacy SSH Hosts"
        description: "Comma-separated SSH hosts; used only when Host Routes is empty"
        placeholder: "laptop.lan, server.lan"
        defaultValue: ""
    }

    StringSetting {
        settingKey: "host_routes"
        label: "Host Routes"
        description: "Comma-separated name=preferred|fallback routes; the local machine is always included"
        placeholder: "snap=snap.wg.lan|snap.lan, starship=starship.lan"
        defaultValue: ""
    }

    StringSetting {
        settingKey: "aliases"
        label: "Legacy Host Aliases"
        description: "Comma-separated source=display mappings for Legacy SSH Hosts"
        placeholder: "80h1vv3=snap"
        defaultValue: ""
    }

    StringSetting {
        settingKey: "terminal"
        label: "Terminal"
        placeholder: Quickshell.env("TERMINAL") || "ghostty"
        defaultValue: Quickshell.env("TERMINAL") || "ghostty"
    }

    StringSetting {
        settingKey: "max_sessions"
        label: "Maximum Sessions"
        placeholder: "20"
        defaultValue: "20"
    }

    StringSetting {
        settingKey: "refresh_seconds"
        label: "Cache TTL"
        description: "Minimum seconds between on-demand session queries"
        placeholder: "30"
        defaultValue: "30"
    }

    StringSetting {
        settingKey: "ssh_connect_timeout"
        label: "SSH Connect Timeout"
        description: "Seconds allowed to establish each SSH connection"
        placeholder: "2"
        defaultValue: "2"
    }

    StringSetting {
        settingKey: "ssh_connection_attempts"
        label: "SSH Connection Attempts"
        description: "Connection attempts before treating a host as unavailable"
        placeholder: "1"
        defaultValue: "1"
    }
}

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
        label: "SSH Hosts"
        description: "Comma-separated SSH hosts; the local machine is always included"
        placeholder: "laptop.lan, server.lan"
        defaultValue: ""
    }

    StringSetting {
        settingKey: "aliases"
        label: "Host Aliases"
        description: "Comma-separated source=display mappings"
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
        placeholder: "15"
        defaultValue: "15"
    }
}

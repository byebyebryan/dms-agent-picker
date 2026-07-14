import QtQuick
import Quickshell
import Quickshell.Io

Item {
    id: root

    readonly property string pluginName: "agentSessions"
    readonly property string helper: Quickshell.env("HOME") + "/.local/bin/dms-agent-picker"

    property var pluginService: null
    property string trigger: "agent:"
    property string hosts: ""
    property string aliases: ""
    property string terminal: Quickshell.env("TERMINAL") || "ghostty"
    property int maxSessions: 20
    property int refreshSeconds: 15
    property int sshConnectTimeout: 2
    property int sshConnectionAttempts: 1
    property var sessions: []
    property var errors: []
    property double lastRefreshMs: 0

    signal itemsChanged()

    Component.onCompleted: {
        loadSettings();
        refresh();
    }

    function loadSettings() {
        if (!pluginService)
            return;
        trigger = pluginService.loadPluginData(pluginName, "trigger", "agent:");
        hosts = pluginService.loadPluginData(pluginName, "hosts", "");
        aliases = pluginService.loadPluginData(pluginName, "aliases", "");
        terminal = pluginService.loadPluginData(
            pluginName,
            "terminal",
            Quickshell.env("TERMINAL") || "ghostty"
        );
        maxSessions = boundedInteger(
            pluginService.loadPluginData(pluginName, "max_sessions", 20),
            1,
            100,
            20
        );
        refreshSeconds = boundedInteger(
            pluginService.loadPluginData(pluginName, "refresh_seconds", 15),
            5,
            300,
            15
        );
        sshConnectTimeout = boundedInteger(
            pluginService.loadPluginData(pluginName, "ssh_connect_timeout", 2),
            1,
            30,
            2
        );
        sshConnectionAttempts = boundedInteger(
            pluginService.loadPluginData(pluginName, "ssh_connection_attempts", 1),
            1,
            5,
            1
        );
    }

    function boundedInteger(value, minimum, maximum, fallback) {
        const parsed = parseInt(value);
        if (isNaN(parsed))
            return fallback;
        return Math.max(minimum, Math.min(maximum, parsed));
    }

    function configuredHosts() {
        return hosts
            .split(/[\s,]+/)
            .map(host => host.trim())
            .filter(host => host.length > 0);
    }

    function configuredAliases() {
        return aliases
            .split(/[\s,]+/)
            .map(alias => alias.trim())
            .filter(alias => alias.length > 0);
    }

    function refresh() {
        if (listProcess.running)
            return;
        const command = [
            helper,
            "--ssh-connect-timeout", String(sshConnectTimeout),
            "--ssh-connection-attempts", String(sshConnectionAttempts),
            "list",
            "--limit", String(maxSessions)
        ];
        for (const host of configuredHosts())
            command.push("--host", host);
        for (const alias of configuredAliases())
            command.push("--alias", alias);
        listProcess.command = command;
        listProcess.running = true;
    }

    function applyResult(text) {
        try {
            const result = JSON.parse(text);
            sessions = Array.isArray(result.sessions) ? result.sessions : [];
            errors = Array.isArray(result.errors) ? result.errors : [];
            lastRefreshMs = Date.now();
            itemsChanged();
        } catch (error) {
            console.warn(pluginName + ": invalid helper output: " + String(error));
        }
    }

    function shortenedPath(path) {
        const home = Quickshell.env("HOME");
        if (path === home)
            return "~";
        if (home && path.startsWith(home + "/"))
            return "~/" + path.slice(home.length + 1);
        return path || "~";
    }

    function age(timestamp) {
        if (!timestamp)
            return "unknown";
        const seconds = Math.max(0, Math.floor(Date.now() / 1000) - timestamp);
        if (seconds < 60)
            return "now";
        const minutes = Math.floor(seconds / 60);
        if (minutes < 60)
            return minutes + "m";
        const hours = Math.floor(minutes / 60);
        if (hours < 24)
            return hours + "h";
        const days = Math.floor(hours / 24);
        if (days < 30)
            return days + "d";
        return Math.floor(days / 30) + "mo";
    }

    function matches(session, query) {
        if (!query)
            return true;
        const haystack = [
            session.name,
            session.host,
            session.connectHost,
            session.windowHost,
            session.cwd,
            session.active ? "active" : "idle"
        ].join(" ").toLowerCase();
        return haystack.includes(query.toLowerCase());
    }

    function getItems(query) {
        if (!listProcess.running && Date.now() - lastRefreshMs > refreshSeconds * 1000)
            refresh();

        const items = [];
        let index = 0;
        for (const session of sessions) {
            if (!matches(session, query))
                continue;
            items.push({
                name: session.name,
                icon: session.active ? "material:terminal" : "material:history",
                comment: session.host + " | " + shortenedPath(session.cwd)
                    + " | " + age(session.recencyAt),
                action: "agent:" + session.host + ":" + session.id,
                categories: ["Agent Sessions"],
                _preScored: 2000 - index,
                _connectHost: session.connectHost,
                _windowHost: session.windowHost || session.host,
                _threadId: session.id,
                _name: session.name,
                _cwd: session.cwd
            });
            index += 1;
        }
        return items;
    }

    function executeItem(item) {
        if (!item || !item._threadId)
            return;
        Quickshell.execDetached([
            helper,
            "--ssh-connect-timeout", String(sshConnectTimeout),
            "--ssh-connection-attempts", String(sshConnectionAttempts),
            "open",
            "--host", item._connectHost,
            "--window-host", item._windowHost,
            "--id", item._threadId,
            "--name", item._name,
            "--cwd", item._cwd,
            "--terminal", terminal
        ]);
    }

    Process {
        id: listProcess
        running: false

        onExited: exitCode => {
            if (exitCode !== 0)
                console.warn(root.pluginName + ": helper exited with " + exitCode);
        }

        stdout: StdioCollector {
            onStreamFinished: root.applyResult(text)
        }

        stderr: StdioCollector {
            onStreamFinished: {
                if (text.trim().length > 0)
                    console.warn(root.pluginName + ": " + text.trim());
            }
        }
    }
}

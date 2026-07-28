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

    function matches(agent, query) {
        if (!query)
            return true;
        const haystack = [
            agent.kind,
            agent.name,
            agent.host,
            agent.connectHost,
            agent.windowHost,
            agent.cwd,
            agent.active ? "active" : "idle"
        ].join(" ").toLowerCase();
        return haystack.includes(query.toLowerCase());
    }

    function activityLabel(session) {
        if (session.activityState === "unknown")
            return "activity unknown";
        return session.active ? "active" : "idle";
    }

    function hostIssues() {
        const grouped = {};
        for (const error of errors) {
            if (!error || !error.host)
                continue;
            const host = String(error.host);
            if (!grouped[host])
                grouped[host] = [];
            if (error.stage && !grouped[host].includes(error.stage))
                grouped[host].push(error.stage);
        }

        const issues = [];
        for (const host of Object.keys(grouped).sort()) {
            const stages = grouped[host];
            const activityUnavailable = stages.includes("active");
            const sessionsUnavailable = stages.includes("threads")
                && stages.includes("claude");
            if (!activityUnavailable && !sessionsUnavailable)
                continue;
            issues.push({
                host: host,
                name: sessionsUnavailable
                    ? "Agent sessions unavailable"
                    : "Agent activity unavailable",
                comment: sessionsUnavailable
                    ? host + " | retrying on the next refresh"
                    : host + " | activity state may be stale"
            });
        }
        return issues;
    }

    function getItems(query) {
        if (!listProcess.running && Date.now() - lastRefreshMs > refreshSeconds * 1000)
            refresh();

        const items = [];
        let issueIndex = 0;
        for (const issue of hostIssues()) {
            if (!matches({
                kind: "status",
                name: issue.name,
                host: issue.host,
                connectHost: issue.host,
                windowHost: issue.host,
                cwd: "",
                active: false
            }, query)) {
                continue;
            }
            items.push({
                name: issue.name,
                icon: "material:warning",
                badgeLabel: "Unavailable",
                comment: issue.comment,
                action: "agent:status:" + issue.host,
                categories: ["Agent Sessions"],
                _preScored: 3000 - issueIndex,
                _kind: "status"
            });
            issueIndex += 1;
        }

        let index = 0;
        for (const session of sessions) {
            if (!matches(session, query))
                continue;
            const agentName = session.kind === "claude" ? "Claude" : "Codex";
            items.push({
                name: session.name,
                icon: session.active
                    ? "material:terminal"
                    : session.activityState === "unknown"
                        ? "material:help"
                        : "material:history",
                badgeLabel: agentName,
                comment: session.host + " | " + shortenedPath(session.cwd)
                    + " | " + age(session.recencyAt) + " | " + activityLabel(session),
                action: "agent:" + session.host + ":" + session.kind + ":" + session.id,
                categories: ["Agent Sessions"],
                _preScored: 2000 - index,
                _kind: session.kind,
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
        if (item._kind === "claude") {
            Quickshell.execDetached([
                helper,
                "--ssh-connect-timeout", String(sshConnectTimeout),
                "--ssh-connection-attempts", String(sshConnectionAttempts),
                "open-claude",
                "--host", item._connectHost,
                "--window-host", item._windowHost,
                "--id", item._threadId,
                "--name", item._name,
                "--cwd", item._cwd,
                "--terminal", terminal
            ]);
            return;
        }
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

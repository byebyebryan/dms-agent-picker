import QtQuick
import Quickshell
import Quickshell.Io
import qs.Services

Item {
    id: root

    readonly property string pluginName: "agentPicker"
    readonly property string helper: Quickshell.env("HOME") + "/.local/bin/dms-agent-picker"

    property var pluginService: null
    property string trigger: "agent:"
    property string hosts: ""
    property string hostRoutes: ""
    property string aliases: ""
    property string terminal: Quickshell.env("TERMINAL") || "ghostty"
    property int maxSessions: 20
    property int refreshSeconds: 30
    property int sshConnectTimeout: 2
    property int sshConnectionAttempts: 1
    property var sessions: []
    property var errors: []
    property var hostSnapshots: ({})
    property var refreshHosts: []
    property var pendingHosts: []
    property var refreshFailures: ({})
    property bool receivedRefreshFinished: false
    property bool streamMalformed: false
    property bool showingUnavailableNotice: false
    property double lastRefreshMs: 0
    readonly property int unavailableNoticeSeconds: 3

    signal itemsChanged()

    Timer {
        id: launcherUpdateDebounce

        interval: 25
        repeat: false
        onTriggered: {
            if (root.pluginService && root.pluginService.requestLauncherUpdate)
                root.pluginService.requestLauncherUpdate(root.pluginName);
        }
    }

    Timer {
        id: unavailableNoticeTimer

        interval: root.unavailableNoticeSeconds * 1000
        repeat: false
        onTriggered: {
            root.showingUnavailableNotice = false;
            root.itemsChanged();
        }
    }

    onItemsChanged: launcherUpdateDebounce.restart()

    Component.onCompleted: {
        loadSettings();
        refresh();
    }

    function loadSettings() {
        if (!pluginService)
            return;
        trigger = pluginService.loadPluginData(pluginName, "trigger", "agent:");
        hosts = pluginService.loadPluginData(pluginName, "hosts", "");
        hostRoutes = pluginService.loadPluginData(pluginName, "host_routes", "");
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
            pluginService.loadPluginData(pluginName, "refresh_seconds", 30),
            5,
            300,
            30
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

    function configuredRoutes() {
        return hostRoutes
            .split(/[\n,]+/)
            .map(route => route.trim())
            .filter(route => route.length > 0);
    }

    function routeKey(route) {
        const separator = route.indexOf("=");
        return (separator >= 0 ? route.slice(0, separator) : route).trim();
    }

    function configuredAliases() {
        return aliases
            .split(/[\s,]+/)
            .map(alias => alias.trim())
            .filter(alias => alias.length > 0);
    }

    function requestedHostKeys() {
        const requested = ["local"];
        const routes = configuredRoutes();
        if (routes.length > 0) {
            for (const route of routes) {
                const key = routeKey(route);
                if (key.length > 0 && !requested.includes(key))
                    requested.push(key);
            }
            return requested;
        }
        for (const host of configuredHosts()) {
            if (!requested.includes(host))
                requested.push(host);
        }
        return requested;
    }

    function refresh() {
        if (listProcess.running)
            return;
        beginRefresh(requestedHostKeys());
        const command = [
            helper,
            "--ssh-connect-timeout", String(sshConnectTimeout),
            "--ssh-connection-attempts", String(sshConnectionAttempts),
            "list",
            "--limit", String(maxSessions),
            "--stream"
        ];
        const routes = configuredRoutes();
        if (routes.length > 0) {
            for (const route of routes)
                command.push("--route", route);
        } else {
            for (const host of configuredHosts())
                command.push("--host", host);
        }
        for (const alias of configuredAliases())
            command.push("--alias", alias);
        listProcess.command = command;
        listProcess.running = true;
    }

    function beginRefresh(hostsToRefresh) {
        const nextSnapshots = {};
        for (const host of hostsToRefresh) {
            if (hostSnapshots[host])
                nextSnapshots[host] = hostSnapshots[host];
        }
        hostSnapshots = nextSnapshots;
        refreshHosts = hostsToRefresh.slice();
        pendingHosts = hostsToRefresh.slice();
        refreshFailures = {};
        receivedRefreshFinished = false;
        streamMalformed = false;
        showingUnavailableNotice = false;
        unavailableNoticeTimer.stop();
        rebuildResults();
    }

    function completeHost(host, result) {
        if (!host || !refreshHosts.includes(host)) {
            console.warn(pluginName + ": unexpected completed host: " + String(host));
            streamMalformed = true;
            return;
        }
        const nextSnapshots = {};
        for (const key of Object.keys(hostSnapshots))
            nextSnapshots[key] = hostSnapshots[key];
        nextSnapshots[host] = {
            sessions: Array.isArray(result.sessions) ? result.sessions : [],
            errors: Array.isArray(result.errors) ? result.errors : []
        };
        hostSnapshots = nextSnapshots;
        pendingHosts = pendingHosts.filter(candidate => candidate !== host);

        const nextFailures = {};
        for (const key of Object.keys(refreshFailures)) {
            if (key !== host)
                nextFailures[key] = refreshFailures[key];
        }
        refreshFailures = nextFailures;
        rebuildResults();
    }

    function finishRefresh() {
        if (pendingHosts.length > 0) {
            streamMalformed = true;
            console.warn(pluginName + ": helper finished with hosts still pending");
            return;
        }
        receivedRefreshFinished = true;
        lastRefreshMs = Date.now();
        updateUnavailableNotice();
        itemsChanged();
    }

    function failPendingRefresh(message) {
        const affectedHosts = pendingHosts.length > 0 ? pendingHosts : refreshHosts;
        if (affectedHosts.length === 0)
            return;
        const nextFailures = {};
        for (const key of Object.keys(refreshFailures))
            nextFailures[key] = refreshFailures[key];
        for (const host of affectedHosts)
            nextFailures[host] = message;
        refreshFailures = nextFailures;
        pendingHosts = [];
        lastRefreshMs = 0;
        updateUnavailableNotice();
        itemsChanged();
    }

    function applyStreamEvent(line) {
        const text = String(line).trim();
        if (!text)
            return;
        try {
            const event = JSON.parse(text);
            if (!event || typeof event.event !== "string")
                throw new Error("missing event name");
            if (event.event === "refresh-started") {
                if (!Array.isArray(event.hosts))
                    throw new Error("refresh-started is missing hosts");
                beginRefresh(event.hosts.map(host => String(host)));
                return;
            }
            if (event.event === "host-complete") {
                completeHost(String(event.host || ""), event);
                return;
            }
            if (event.event === "refresh-finished") {
                finishRefresh();
                return;
            }
            throw new Error("unknown event " + event.event);
        } catch (error) {
            streamMalformed = true;
            console.warn(pluginName + ": invalid helper output: " + String(error));
        }
    }

    function rebuildResults() {
        const candidates = [];
        const combinedErrors = [];
        for (let hostIndex = 0; hostIndex < refreshHosts.length; hostIndex++) {
            const host = refreshHosts[hostIndex];
            const snapshot = hostSnapshots[host];
            if (!snapshot)
                continue;
            const hostSessions = Array.isArray(snapshot.sessions) ? snapshot.sessions : [];
            for (let sessionIndex = 0; sessionIndex < hostSessions.length; sessionIndex++) {
                const session = hostSessions[sessionIndex];
                if (session && typeof session === "object") {
                    candidates.push({
                        session: session,
                        hostIndex: hostIndex,
                        sessionIndex: sessionIndex
                    });
                }
            }
            const hostErrors = Array.isArray(snapshot.errors) ? snapshot.errors : [];
            for (const error of hostErrors) {
                if (error && typeof error === "object")
                    combinedErrors.push(error);
            }
        }

        candidates.sort((left, right) => {
            const recencyDifference = Number(right.session.recencyAt || 0)
                - Number(left.session.recencyAt || 0);
            if (recencyDifference !== 0)
                return recencyDifference;
            const leftId = String(left.session.id || "");
            const rightId = String(right.session.id || "");
            if (leftId !== rightId)
                return leftId < rightId ? 1 : -1;
            if (left.hostIndex !== right.hostIndex)
                return left.hostIndex - right.hostIndex;
            return left.sessionIndex - right.sessionIndex;
        });

        const seen = {};
        const mergedSessions = [];
        for (const candidate of candidates) {
            const session = candidate.session;
            const sessionKey = [
                String(session.windowHost || session.host || ""),
                String(session.kind || ""),
                String(session.id || "")
            ].join("\u0000");
            if (seen[sessionKey])
                continue;
            seen[sessionKey] = true;
            mergedSessions.push(session);
            if (mergedSessions.length >= maxSessions)
                break;
        }
        sessions = mergedSessions;
        errors = combinedErrors;
        itemsChanged();
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

    function hostLabel(host) {
        const snapshot = hostSnapshots[host];
        if (snapshot && Array.isArray(snapshot.sessions)) {
            for (const session of snapshot.sessions) {
                if (session && session.host)
                    return String(session.host);
            }
        }
        const normalizedHost = String(host).toLowerCase();
        for (const alias of configuredAliases()) {
            const separator = alias.indexOf("=");
            if (separator <= 0)
                continue;
            if (alias.slice(0, separator).trim().toLowerCase() === normalizedHost)
                return alias.slice(separator + 1).trim() || host;
        }
        return host;
    }

    function hostList(hostsToDisplay) {
        const labels = hostsToDisplay.map(host => hostLabel(host));
        if (labels.length <= 3)
            return labels.join(", ");
        return labels.slice(0, 3).join(", ") + " +" + String(labels.length - 3);
    }

    function hostProblems() {
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

        const problems = {
            unreachable: [],
            interrupted: [],
            sessions: [],
            activity: []
        };
        for (const host of Object.keys(refreshFailures).sort())
            problems.interrupted.push(host);
        for (const host of Object.keys(grouped).sort()) {
            if (pendingHosts.includes(host) || refreshFailures[host])
                continue;
            const stages = grouped[host];
            const activityUnavailable = stages.includes("active");
            const sessionsUnavailable = stages.includes("threads")
                && stages.includes("claude");
            if (sessionsUnavailable && activityUnavailable) {
                problems.unreachable.push(host);
                continue;
            }
            if (sessionsUnavailable)
                problems.sessions.push(host);
            if (activityUnavailable)
                problems.activity.push(host);
        }
        return problems;
    }

    function hasHostProblems(problems) {
        return problems.unreachable.length > 0
            || problems.interrupted.length > 0
            || problems.sessions.length > 0
            || problems.activity.length > 0;
    }

    function hostProblemComment(problems) {
        const details = [];
        if (problems.unreachable.length > 0)
            details.push(hostList(problems.unreachable) + " unreachable");
        if (problems.interrupted.length > 0)
            details.push("discovery interrupted for " + hostList(problems.interrupted));
        if (problems.sessions.length > 0)
            details.push("sessions unavailable on " + hostList(problems.sessions));
        if (problems.activity.length > 0)
            details.push("activity unknown on " + hostList(problems.activity));
        return details.join("; ");
    }

    function updateUnavailableNotice() {
        const problems = hostProblems();
        showingUnavailableNotice = hasHostProblems(problems);
        if (showingUnavailableNotice)
            unavailableNoticeTimer.restart();
        else
            unavailableNoticeTimer.stop();
    }

    function statusIssue() {
        const problems = hostProblems();
        if (pendingHosts.length > 0) {
            const refreshComment = "Checking " + hostList(pendingHosts)
                + " | showing last known sessions";
            const problemComment = hostProblemComment(problems);
            return {
                host: pendingHosts[0],
                name: "Refreshing Agent Picker…",
                comment: problemComment.length > 0
                    ? refreshComment + "; " + problemComment
                    : refreshComment,
                badgeLabel: "Refreshing",
                icon: "material:refresh"
            };
        }
        if (showingUnavailableNotice && hasHostProblems(problems)) {
            return {
                host: problems.unreachable[0] || problems.interrupted[0]
                    || problems.sessions[0] || problems.activity[0],
                name: problems.unreachable.length > 0
                    ? "Agent host unavailable"
                    : "Agent refresh unavailable",
                comment: hostProblemComment(problems),
                badgeLabel: "Unavailable",
                icon: "material:warning"
            };
        }
        return null;
    }

    function getItems(query) {
        if (!listProcess.running && Date.now() - lastRefreshMs > refreshSeconds * 1000)
            refresh();

        const items = [];
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
                categories: ["Agent Picker"],
                _preScored: 2000 - index,
                _kind: session.kind,
                _connectHost: session.connectHost,
                _route: session.route || "",
                _windowHost: session.windowHost || session.host,
                _threadId: session.id,
                _name: session.name,
                _cwd: session.cwd
            });
            index += 1;
        }

        const issue = statusIssue();
        if (issue && matches({
            kind: "status",
            name: issue.name,
            host: issue.host,
            connectHost: issue.host,
            windowHost: issue.host,
            cwd: "",
            active: false
        }, query)) {
            // DMS launcher plugins expose only selectable result rows. Keep
            // the informational row immediately after the first matching
            // session, so it never receives initial keyboard focus.
            const score = items.length > 0 ? items[0]._preScored - 0.5 : 4000;
            items.push({
                name: issue.name,
                icon: issue.icon,
                badgeLabel: issue.badgeLabel,
                comment: issue.comment,
                action: "agent:status:" + issue.host,
                categories: ["Agent Picker"],
                _preScored: score,
                _kind: "status"
            });
        }
        return items;
    }

    function executeItem(item) {
        if (!item || !item._threadId)
            return;
        if (openProcess.running) {
            ToastService.showWarning(
                "Agent Picker",
                "Another session is still being prepared"
            );
            return;
        }
        const command = [
            helper,
            "--ssh-connect-timeout", String(sshConnectTimeout),
            "--ssh-connection-attempts", String(sshConnectionAttempts),
            item._kind === "claude" ? "open-claude" : "open",
            "--host", item._route || item._connectHost,
            "--window-host", item._windowHost,
            "--id", item._threadId,
            "--name", item._name,
            "--cwd", item._cwd,
            "--terminal", terminal,
            "--detach"
        ];
        openProcess.errorText = "";
        openProcess.targetName = item._name || "session";
        openProcess.command = command;
        openProcess.running = true;
    }

    Process {
        id: openProcess

        property string errorText: ""
        property string targetName: "session"

        running: false

        onExited: exitCode => {
            if (exitCode === 0)
                return;
            Qt.callLater(() => {
                const detail = errorText || "helper exited with " + exitCode;
                console.warn(root.pluginName + ": failed to open " + targetName + ": " + detail);
                ToastService.showError(
                    "Agent Picker could not open " + targetName,
                    detail,
                    "",
                    "agent-picker-open"
                );
            });
        }

        stderr: StdioCollector {
            onStreamFinished: openProcess.errorText = text.trim()
        }
    }

    Process {
        id: listProcess
        running: false

        onExited: exitCode => {
            if (exitCode !== 0)
                console.warn(root.pluginName + ": helper exited with " + exitCode);
            if (!root.receivedRefreshFinished || root.pendingHosts.length > 0) {
                const message = root.streamMalformed
                    ? "helper emitted invalid refresh data"
                    : exitCode === 0
                        ? "helper ended before completing refresh"
                        : "helper exited with " + exitCode;
                root.failPendingRefresh(message);
            } else if (root.streamMalformed) {
                root.lastRefreshMs = 0;
                root.itemsChanged();
            }
        }

        stdout: SplitParser {
            splitMarker: "\n"
            onRead: line => root.applyStreamEvent(line)
        }

        stderr: StdioCollector {
            onStreamFinished: {
                if (text.trim().length > 0)
                    console.warn(root.pluginName + ": " + text.trim());
            }
        }
    }
}

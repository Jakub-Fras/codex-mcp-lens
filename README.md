# MCP Lens

**See which apps and MCP servers Codex knows about, where they came from, and when Codex last asked them to do something.**

It is easy to add a tool to Codex and forget about it. MCP Lens gives you one local report to review those tools. It shows the latest requests it can find, points you to the setting or plugin behind each entry, and explains what you can turn off or remove. You do not need to know how to read a Codex configuration file.

MCP Lens is for reviewing **your own Codex setup**. It does not scan MCP servers for vulnerabilities or decide that a tool is safe.

## Get started

On a Mac, double-click [Open MCP Lens.command](./Open%20MCP%20Lens.command). You need Python 3.11 or newer. For the fullest report, the `codex` command must also work on your computer. If macOS blocks the downloaded launcher, run this from Terminal in the MCP Lens folder instead:

```sh
python3 mcp_lens.py report
python3 mcp_lens.py manage --project . --report mcp-lens-report.html
```

The launcher asks whether to check the Codex settings for a particular folder. Choose one if you want to see the settings that apply there and any saved Codex file-edit records for it. Press Return to use your home folder. Connection activity still covers saved Codex history across folders.

The launcher makes `mcp-lens-report.html`, opens it in your browser, and starts a temporary local manager. **Keep the Terminal window open** while using the report's **Turn off** and **Remove** buttons. If you open the saved HTML file directly, you can read it but cannot change settings from it. The launcher also makes `mcp-lens-share.html`, a separate counts-only summary.

Want to see the report before checking your own setup? Open [the fictional demo](./mcp-lens-demo.html). It uses invented data.

## Why look at these connections?

An MCP server gives Codex tools supplied by another program or service. Depending on the tool and its permissions, it could read information, change something in an account, or run code on your computer. A tool can also return content that tries to influence what an AI assistant does. [Red Hat's overview of MCP risks](https://www.redhat.com/en/blog/model-context-protocol-mcp-understanding-security-risks-and-controls) explains these issues in more depth.

That does not mean every connection is dangerous. It means you should be able to answer a few ordinary questions: **What did I add? Is it still on? Has Codex asked it to do anything recently? Do I still need it?** MCP Lens brings together the local records that can help you answer them.

## What you will see

- **Apps (connectors):** Apps Codex lists as on, plus apps with matching requests in saved history. An app being on in Codex does not confirm that its outside account is connected.
- **MCP servers:** Entries found in Codex settings or saved plugin files. A plugin file on your computer does not, by itself, prove that its server is enabled or running.
- **Recorded activity:** The latest matching requests, dates, tool names, and recorded outcomes. The report does not copy request contents or results. A failed request still counts as a request.
- **Where an entry came from:** The settings entry or plugin file when MCP Lens can identify it, along with the next place to look in Codex.
- **Changes in a chosen folder:** Saved Codex `fileChange` records for paths inside that folder. These are recorded edits, not a complete audit of everything that changed on disk.

Open any entry in the report to see what MCP Lens found, what it could not confirm, and the action available for that entry. A red triangle is a reminder to review; it is not a finding that a tool is unsafe.

### About the activity dates

“No matching request” means MCP Lens found none in the saved Codex records it could read. It does **not** mean the tool was never used. History may be missing or rotated, and some older calls do not contain enough information to assign them to a particular app. “First observed” is the earliest matching saved request, not the install date. If a file or record could not be read, the report says so.

### About access and permissions

The report can show local tool settings, approval rules, saved tool names, and descriptions when Codex provides them. Those are clues, not a complete list of what an outside account or MCP server can access. MCP Lens cannot verify the permissions granted inside Notion, GitHub, or another service; it does not run servers, inspect their code, or detect prompt injection. Server-provided descriptions and safety hints may be wrong or out of date.

## Turn something off or remove it

The buttons appear only for entries MCP Lens can change through the local report manager. For a directly configured MCP server, **Turn off** leaves its entry in Codex settings but disables it; **Remove** deletes that settings entry. A plugin-provided MCP server may be turned off here. To uninstall its plugin, type `/plugins` in Codex and use **Uninstall plugin** if that option is available. Some apps can be turned off in local Codex settings; each report entry tells you what is supported.

These actions change Codex settings on this computer. They do not uninstall a local program, delete data held by a service, or disconnect an outside account. For an app account connection, follow the separate account step shown in its report entry and review the authorized apps in the service's own settings. MCP Lens makes no backup file when changing settings.

## More ways to run it

Choose a folder and make a counts-only copy to share:

```sh
python3 mcp_lens.py report --project /path/to/folder --share-output mcp-lens-share.html
```

Save a local baseline, then use the same path on a later run to compare entries and saved connector tool lists:

```sh
python3 mcp_lens.py report --snapshot mcp-lens-snapshot.json
```

The snapshot stores entry IDs, tool names, and fingerprints. It does not store endpoints, command arguments, tool descriptions, credentials, or request contents. A changed tool list is a reason to review, not proof of malicious behavior.

Review MCP entries whose **last matching saved request** is more than 90 days old:

```sh
python3 mcp_lens.py cleanup --older-than 90
```

This is a dry run. To choose entries one by one and confirm each change, add `--apply`. You can also change one entry with `python3 mcp_lens.py remove global:SERVER_NAME`, using its exact inventory ID. An old activity date does not prove you no longer use a server. Entries with no matching retained activity are not cleanup candidates; cleanup review stops if some history could not be read.

Other options: `--offline` skips Codex's effective-settings and app-status checks; `--home PATH` reads another Codex home; `--config PATH` reads a particular settings file and skips those Codex checks. `--no-animation` stops the moving Terminal companion, and `NO_COLOR=1` disables Terminal colour. `python3 mcp_lens.py demo` regenerates the fictional demo.

## How MCP Lens gets its information

MCP Lens asks Codex for the settings that apply to the chosen folder, its general app list, and, when available, discovered skills and hooks. It also reads local settings, plugin manifests, cached app tool metadata, saved history databases, and session logs under the Codex home directory. It does not contact MCP servers to test them. The global app directory is a catalogue; MCP Lens does not treat every app in it as a connection you made.

Saved app requests can appear under Codex's shared `codex_apps` server. MCP Lens uses the recorded connector ID when present and older tool names when it can attribute them without guessing. It matches call IDs across history sources to avoid counting the same request twice. The chosen folder affects the settings check; activity counts can include conversations started elsewhere, and the report labels that distinction. It also keeps built-in Codex features separate from outside apps and MCP servers.

The report's review prompts draw on the [OWASP MCP Top 10](https://owasp.org/projects/mcp-top-10) and [OWASP guidance for third-party MCP servers](https://genai.owasp.org/resource/cheatsheet-a-practical-guide-for-securely-using-third-party-mcp-servers-1-0/). They explain why to look more closely; they are not vulnerability findings or risk scores. Codex's [plugin policy documentation](https://developers.openai.com/plugins/build/plugins) and [app server documentation](https://learn.chatgpt.com/docs/app-server) describe some of the local settings MCP Lens can show. A visible setting does not establish the permissions ultimately granted by a remote account.

## Contributing and privacy

MCP Lens is open source under the [MIT License](./LICENSE). To run the local fixture checks:

```sh
python3 -m unittest discover -s tests -v
```

The fixtures cover local behavior and a simulated Codex app server. They cannot verify every Codex version or outside account.

**Keep your generated report to yourself unless you have reviewed it.** It can contain app names, file names, folder paths, and activity from your computer. The counts-only share file is a different output. Before publishing a fork or release bundle, check its actual contents: `.gitignore` cannot protect a manual ZIP, a custom output path, or a file already committed to Git history.

---
icon: lucide/wrench
---

# Tools

76 tools in three groups. Every tool's MCP `annotations` declare whether it is
read-only, mutating, or destructive; the Kind column mirrors that. Every tool
returns markdown plus `structured_content` validated against a published
`outputSchema`. Errors come back as `is_error` results with recovery hints.

## Server lifecycle (`tools/jupyterhub.py`)

| Tool                    | Does                                                             | Kind        |
| ----------------------- | ---------------------------------------------------------------- | ----------- |
| `create_jupyter_server` | Start a named server and wait until it is ready, with progress   | mutating    |
| `list_jupyter_servers`  | List your named servers, stopped ones included                   | read-only   |
| `get_jupyter_server`    | Show one server's status, image, resources, and URL              | read-only   |
| `stop_jupyter_server`   | Stop a server but keep its entry (it still counts toward the 10) | destructive |
| `delete_jupyter_server` | Stop a server and remove its entry                               | destructive |
| `list_supported_images` | List the CPU and GPU images `create_jupyter_server` accepts      | read-only   |
| `list_sites`            | List the site names `create_jupyter_server` accepts              | read-only   |

The owner of every server is always the hub user the caller's token belongs to.
No tool takes an owner argument.

### `create_jupyter_server`

`create_jupyter_server(image, name=None, site="local", cpu_cores=1, memory_gb=1, guaranteed_qos=False, gpus=0, gpu_product=None)`

- `image` must be on the allowlist: a GPU image when `gpus > 0`, a CPU image
  otherwise (`list_supported_images`).
- `site` must be one of `list_sites`.
- `cpu_cores` can be 1 to 16 and `memory_gb` 1 to 256.
- `name` must be a DNS label (lowercase letters, digits and `-`, at most 63
  characters). If it is omitted, the first free `notebook-<n>` is used. Giving
  the name of one of your stopped servers starts it again with the new settings;
  an active server's name is a conflict.
- `guaranteed_qos=True` reserves exactly `cpu_cores` and `memory_gb` (requests =
  limits) instead of a burstable allocation.
- `gpu_product` pins a GPU model. If it is omitted, any model is accepted.

The request is sent as the hub spawner's `user_options`, with string values, as
the BinderHub form sends them:

```json
{
  "image": "hub.opensciencegrid.org/usatlas/ml-platform-cpu:latest",
  "resource_requests": {
    "site": "local",
    "cpu": "1",
    "memory": "1",
    "qos": "Burstable",
    "gpuCount": "0"
  }
}
```

When the hub answers `202` (pending), the tool follows
`/hub/api/users/<you>/servers/<name>/progress` and forwards each event as an MCP
progress notification (`progress` out of 100, plus the hub's message). It
returns when the hub reports ready, and fails if the hub reports failure or the
launch timeout passes (900 s by default). After a timeout the server keeps
spawning and is not deleted. The result includes the server's JupyterLab URL on
the public hub.

### Named-server limit

Each user can have at most 10 named servers, and stopped ones count.

- `stop_jupyter_server` stops the server and keeps its entry, which can be
  started again with `create_jupyter_server(name=...)`.
- `delete_jupyter_server` stops the server and removes the entry (the hub's
  `{"remove": true}`), which frees a slot.

When the limit is reached, `create_jupyter_server` returns the hub's message and
suggests deleting a server.

### Servers launched from BinderHub

Servers started from the BinderHub UI (names like
`maniaclab-ml_platform-ab12cd34`) are listed with their `binder_request` and
work with every `nb_*` tool. BinderHub stores a `token` in their `user_options`;
it is never shown.

## Notebook content proxy (`tools/nb_proxy.py`, upstream: [jupyter-mcp-server](https://github.com/datalayer/jupyter-mcp-server))

Every `nb_*` tool takes `server_name` first. It looks the server up among the
caller's own named servers and checks that it is ready (not pending or stopped).
Then it forwards the call to the server's own `jupyter-mcp-server` through the
hub proxy (`/user/<you>/<server_name>/mcp`), authenticated with the caller's hub
token in a header. The token is never returned to the caller.

| Tool                          | Does                                                    | Kind        |
| ----------------------------- | ------------------------------------------------------- | ----------- |
| `nb_list_files`               | List files on the notebook server's filesystem          | read-only   |
| `nb_list_kernels`             | List all running kernels on the notebook server         | read-only   |
| `nb_list_notebooks`           | List notebooks open on the notebook server              | read-only   |
| `nb_use_notebook`             | Connect to or create a notebook on the notebook server  | mutating    |
| `nb_unuse_notebook`           | Disconnect from a notebook on the notebook server       | mutating    |
| `nb_restart_notebook`         | Restart a notebook's kernel on the notebook server      | destructive |
| `nb_read_notebook`            | Read a notebook's cells from the notebook server        | read-only   |
| `nb_read_cell`                | Read a cell from the active notebook                    | read-only   |
| `nb_insert_cell`              | Insert a cell at a given index in the active notebook   | mutating    |
| `nb_overwrite_cell_source`    | Overwrite the source of a cell in the active notebook   | destructive |
| `nb_edit_cell_source`         | Edit part of a cell's source in the active notebook     | mutating    |
| `nb_delete_cell`              | Delete one or more cells from the active notebook       | destructive |
| `nb_move_cell`                | Move a cell to a different index in the active notebook | mutating    |
| `nb_execute_cell`             | Execute a specific cell in the active notebook          | destructive |
| `nb_insert_execute_code_cell` | Insert a code cell and immediately execute it           | destructive |
| `nb_execute_code`             | Execute arbitrary code in the notebook server's kernel  | destructive |
| `nb_clear_cell_output`        | Clear a code cell's outputs, keeping the cell           | destructive |

The three code-execution tools (`nb_execute_cell`,
`nb_insert_execute_code_cell`, `nb_execute_code`) are marked destructive even
though "execute" isn't literally a delete: they can mutate anything the kernel
can reach, which is what the annotation communicates to a client.
Cell-addressing tools accept either a positional index or the cell's stable
`cell_id` (given both, the id wins); prefer ids, since an index goes stale as
soon as a cell is inserted above it.

## JupyterLab UI proxy (`tools/nb_ui.py`, upstream: [jupyter-mcp-tools](https://github.com/datalayer/jupyter-mcp-tools))

Every `nb_ui_*` tool proxies one JupyterLab command through the same
ownership/readiness/token path as the `nb_*` tools, but the command runs
**inside the user's open JupyterLab browser tab** (relayed over a websocket by
the `jupyter-mcp-tools` extension) and acts on the active notebook, console, or
selection there. With no tab open, the call fails. The notebook image must
allowlist each command
([maniaclab/ml_platform#14](https://github.com/maniaclab/ml_platform/issues/14)).

Tool names are `nb_ui_` + the upstream id with `-` → `_` (upstream ids are
JupyterLab command ids with `:` → `_`, e.g. `notebook:run-all-cells` →
`notebook_run-all-cells` → `nb_ui_notebook_run_all_cells`). Tools that take
arguments beyond `server_name` are noted.

| Area         | Tools (`nb_ui_` prefix omitted)                                                                                                                                                                                                                                                                              | Kind        |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------- |
| Notebook     | `notebook_get_selected_cell`, `notebook_move_cursor_down`, `notebook_move_cursor_up`, `notebook_extend_marked_cells_below`, `notebook_extend_marked_cells_above`, `notebook_copy_cell`                                                                                                                       | read-only   |
| Notebook     | `notebook_insert_cell_below`, `notebook_insert_cell_above`, `notebook_paste_cell_below`, `notebook_paste_cell_above`, `notebook_move_cell_up`, `notebook_move_cell_down`, `notebook_split_cell_at_cursor`, `notebook_change_cell_to_code`, `notebook_change_cell_to_markdown`, `notebook_change_cell_to_raw` | mutating    |
| Notebook     | `notebook_delete_cell`, `notebook_cut_cell`, `notebook_merge_cell_above`, `notebook_merge_cell_below`, `notebook_run_all_cells`, `notebook_run_cell`, `notebook_run_cell_and_select_next`, `notebook_run_cell_and_insert_below`, `notebook_append_execute` (`source`, `cell_type`)                           | destructive |
| Console      | `console_create` (`path`, `insert_mode`, `activate`)                                                                                                                                                                                                                                                         | mutating    |
| Console      | `console_clear`, `console_interrupt_kernel`, `console_inject` (`code`, `path`, `activate`)                                                                                                                                                                                                                   | destructive |
| Documents    | `docmanager_open` (`path`, `factory`), `docmanager_new_untitled` (`content_type`, `path`, `ext`), `docmanager_save`, `docmanager_duplicate`                                                                                                                                                                  | mutating    |
| File browser | `filebrowser_go_to_path` (`path`), `filebrowser_refresh`, `filebrowser_toggle_hidden_files`                                                                                                                                                                                                                  | read-only   |
| File browser | `filebrowser_create_new_directory`                                                                                                                                                                                                                                                                           | mutating    |
| Kernel       | `kernelmenu_reconnect_to_kernel`                                                                                                                                                                                                                                                                             | mutating    |
| Kernel       | `kernelmenu_interrupt`, `kernelmenu_shutdown`                                                                                                                                                                                                                                                                | destructive |
| UI           | `application_toggle_left_area`, `application_toggle_right_area`, `application_toggle_presentation_mode`, `apputils_change_theme` (`theme`), `editmenu_open`, `filemenu_open`, `helpmenu_open`                                                                                                                | read-only   |
| Search       | `documentsearch_start` (`search_text`), `documentsearch_highlightNext`, `documentsearch_highlightPrevious`                                                                                                                                                                                                   | read-only   |
| Terminal     | `terminal_create_new`, `terminal_refresh`                                                                                                                                                                                                                                                                    | mutating    |

"Read-only" here includes commands that only change view state (cursor,
selection, layout, search, theme). Commands that execute code are destructive,
like the `nb_execute_*` tools.

Deliberately **not** proxied (and not allowlisted in the image):

- `filebrowser_upload` / `filebrowser_download` — they open a file picker or
  save into the human's browser; no bytes ever reach the MCP client.
- `docmanager_delete` / `docmanager_rename` / `docmanager_save-as`,
  `kernelmenu_change`, `kernelmenu_restart`, `console_restart-kernel` — they
  block on a modal dialog that needs a human click. Use `nb_restart_notebook` to
  restart a kernel without the UI.

## Images with different tool sets

The tool list above is fixed, but the server behind a call can run any image.
Servers launched from the BinderHub UI run BinderHub-built images, and older
images offer fewer tools: images with jupyter-mcp-server 1.x, such as the
2026.8.17 ml-platform images, lack `clear_cell_output` and all but two UI
commands. The chart value `notebook.images.toolOverrides` maps an **exact image
string**, as found in the server's `user_options.image`, to the upstream
jupyter-mcp-server tool ids that image offers (e.g. `read_cell`,
`notebook_run-all-cells`). Images without an entry are assumed to support every
tool, matching the latest ml-platform image. Calling a tool the server's image
does not offer returns a clear "not supported by image" error naming the image,
without contacting the server.

On a jupyter-mcp-server 1.x image, the 2.x `cell_id`-style arguments are not
understood: 1.x silently drops unknown arguments, so passing **both**
`cell_index` and `cell_id` uses the index (2.x would use the id), and passing
`cell_id` alone fails upstream because `cell_index` is required there.

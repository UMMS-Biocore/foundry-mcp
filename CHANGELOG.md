# Changelog

## [Unreleased]

### Added
- **Chains of workspace steps**
  - `get_started` has a fourth recipe, `chain_workspace_steps` (topic
    "chain"): when no step needs a person's decision, a chain of steps runs as
    one workspace job, a script under `set -e` whose gate steps end with
    `exit 1`. Agents offer it before asking a user to keep a session open, and
    say the trade-off: every step gets the largest step's resources, and nobody
    reviews a result between steps.
- **Run advanced settings**
  - `get_run_advanced_settings` reads a run's container (docker, singularity
    or off, with image and run options), its executor settings for all
    processes (queue, memory, cpu, time, cluster options) and its per process
    overrides, and says whether the run can still be edited. It writes nothing.
  - `update_run_advanced_settings` takes `confirm_update` and changes them in
    place on the run. Only the values given change: exec_all and each
    process_exec row merge onto the saved ones, null removes an override, and
    a container change always sends both the Docker and Singularity switches.
    It refuses a run that has been launched, and returns the settings before
    the save and as read back after it. Checked live on run 12388, a duplicate
    of run 12366.
- **Build pipelines from processes**
  - `get_pipeline` reads a pipeline's fields and its graph as the editor saves
    it, and spells out every connection by process, port name and slot.
  - `create_pipeline_config` previews a new pipeline from processes, pipeline
    inputs and outputs, and connections between them. Each connection is type
    checked (file type must match; a value connects only to a value), a refusal
    names the node by the caller's key, and process inputs nothing feeds are
    listed. It writes nothing.
  - `create_pipeline` takes the same arguments plus `confirm_create`, rebuilds
    and rechecks the graph, and creates a new private pipeline. It never saves
    over an existing one.
  - `list_pipeline_revisions` lists every revision of a pipeline. Each
    revision has its own pipeline id (1545 is v1.0.3, 1776 is v1.0.4).
  - `update_pipeline` takes the same graph arguments plus `confirm_update`
    and saves the rebuilt graph over an existing pipeline in place, keeping
    its other fields. It refuses a pipeline that is public or group shared,
    pinned, or not editable by the signed in account, because a save changes
    it under every run that uses it. It returns the previous edges too.
  - Needs viafoundry_sdk 1.3.0 (`client.pipelines`).
- **Workspaces: run code in a container on a run environment**
  - `workspace_list` and `workspace_create` list and create your workspaces on
    a run environment that has container execution turned on.
  - `workspace_run` runs a command as a job. It returns a `submission_key`;
    calling again with that key reads or retries the same job, and a new key
    starts a new job. Every retry inside one call sends the same key, so a lost
    answer never starts a second job. A job is reported as not submitted only
    when the server refuses it with a 4xx. When the answer is lost on every
    try (a gateway error, no connection, an answer that cannot be read or a
    redirect to an unusable address), a later try is refused with no outcome
    after an earlier one was lost, or the server answers with an outcome this
    client does not know, the call reports the job as `uncertain` with its
    key, so the client can read it back or retry with the same key. A key you
    pass is checked before any request, and a command over 32 KiB is refused
    locally.
  - When the run environment needs a person's approval, `workspace_run` and
    `workspace_job_status` return an `approve_at` link. A person approves in a
    signed in Foundry Connect session; no tool can approve or reject a job.
  - `workspace_job_status` reads a job by its key and `workspace_cancel` stops
    a queued or running one.
  - Refusals (a cap reached, a rejected or expired request, a key reused with a
    different job, a job that cannot be cancelled) are raised as MCP errors
    with the server's `reason` and a sentence saying what to do. A 401 or 403
    says to check the client's token, a lost connection in any workspace tool
    is a clear error (after a `workspace_create` one, check `workspace_list`
    before trying again), and a hostname that is not a full URL is named as
    the problem.
  - A refused `workspace_run` or `workspace_create` now says what kind of
    refusal it was, when the server sends it: `category` (a used up limit is
    `quota` with HTTP 429, a request over a limit is `ceiling` with HTTP 422,
    a folder that is not allowed is `mount_policy` with the `mount` index),
    `retryable`, and the limit's `limit` and `current` figures. A quota or
    ceiling refusal adds a `next_step` sentence (wait and call again, remove
    files or workspaces you no longer need, or ask for less), and a storage
    refusal names disk use in bytes. A refusal from an older server without
    these fields reads as before.
- **Pipelines and processes from git**
  - `connect_pipeline_repository`, `get_pipeline_repository` and
    `export_pipeline_revision` connect a pipeline to a git repository and
    export it, with every process and module it uses, each under its own tag.
  - `check_pipeline_import` reports what importing a pipeline folder would do
    (one status per process, module and the pipeline, and whether it can be
    applied). `import_pipeline_from_repository` imports the revision that was
    checked. `list_pipeline_menu_groups` lists the groups a pipeline can go in.
  - `check_process_import` lists the processes a repository publishes, or
    resolves one revision with its contract and consent text.
    `import_process_from_repository` imports it as a new process, only with
    `confirm_import=True` and the commit id the check reported.
  - Refusals from these tools are raised as MCP errors, so a refused import or
    export never reads as a success.
- **Listing datasets**
  - `list_datasets(scope=...)` lists the datasets you own (`mine`, the
    default), those shared with you or your groups (`shared`), public ones
    (`public`), or all you can read (`all`), with an optional name search and
    paging. Each row gives the dataset id, name, owner, file count, canvas
    ids, dates and tags, so a dataset can be picked as a run's reads input.
    `search_datasets` now says it searches the files inside one dataset.
- **Starting a run from scratch**
  - `list_projects`, `create_project` and `list_pipelines` find or create where a
    run lives and which pipeline it runs.
  - `create_run(pipeline_id, project_id, name)` creates an unlaunched run and
    returns its form, the run environments you can launch on, and a proposed
    execution directory for each (the environment's declared default, else
    `/home/<login>/foundry-connect-runs/<run name>`).
  - `list_run_environments(run_id)` shows the same for an existing run.
  - `update_run` can now set the run environment, launch and publish
    directories, and the run name, and sends only the fields you give it.
    `preflight_run` reports a missing run environment or execution directory
    before launch.
- **Task resources for a run**
  - `update_run(exec_settings=...)` sets the queue, memory, cpu, time and
    scheduler options every task asks for (`globalExec`) and per process
    overrides (`processExec`), in the shape `get_run_details(verbose=True)`
    reports, so settings copied from a run that worked can be saved as is.
  - `update_run(singularity_options=...)` sets the Singularity run options,
    for example extra `-B` binds, and keeps the run's image.
  - `preflight_run` warns (`executor_resources`) when tasks go to a batch
    scheduler such as LSF or Slurm and neither the run nor its environment
    sets memory, since the scheduler's site default can kill large tasks.
- **Public accessions to a sample dataset**
  - `create_dataset_from_accessions(name, accessions, canvas_id="", dry_run=False)`
    turns GEO (GSE, GSM), SRA/ENA/DDBJ (SRP, ERP, DRP, PRJNA, PRJEB, SRX,
    ERX, SRR, ERR, DRR, SAMN, SAMEA) and ArrayExpress (E-MTAB) accessions
    into a sample dataset whose records the run downloads from SRA. Runs are
    resolved through ENA (GEO via NCBI eutils, ArrayExpress via BioStudies)
    and grouped by sample, with the layout from ENA and the total raw size.
    Anything that cannot be resolved is listed with its reason, and mixed
    layouts or non RNA libraries come back as warnings. `dry_run=True`
    returns the plan and writes nothing. The result names the dataset's
    `layout` and the `mate` value the run must carry, and
    `peak_inputs_bytes_estimate` (6x the raw size, measured on a staging run)
    for budgeting storage, since the download writes uncompressed fastq first.
  - `preflight_run` fails (`layout`) when the attached dataset's layout
    disagrees with the run's `mate` input, or mixes layouts.
  - `update_run(cleanup_delay_hours=...)` turns on the cleanup of a run's
    intermediate files and sets how many hours after the run ends it happens.

### Fixed
- `download_file` failed for files larger than about 15MB, because the whole
  file came back base64 encoded in one tool result. A file up to 5MB still
  comes back inline. A larger one now comes back as `download_url` with a
  ready `download_command` (curl) that saves it to disk, and the server stops
  reading it after 5MB instead of holding the whole file in memory.
- `list_pipelines` returned nothing for a user who owns no pipelines. It now
  asks for released, your own and shared pipelines one view at a time and
  merges them, since the server narrows a combined filter to their overlap.
- `create_run` and `list_run_environments` now list the pipeline's inputs in
  `input_definitions` (name, type, required, default, description, options),
  required ones first. A fresh run has no saved inputs, so the form used to
  show none.
- `create_project` now reports the project's name instead of `None`.
- `update_run` no longer refuses a call that leaves out `permission`. It
  keeps the run's saved permission and group, so a client changing only the
  inputs or the environment does not have to look them up first.
- `update_run(exec_settings=...)` now accepts the `processExec` key of a
  process inside a module (`procGnum-<n>p<module>`, as
  `get_run_details(verbose=True)` reports it). It used to refuse every
  process that a pipeline pulls in through a module.
- A refused or failed `update_run` now comes back as an MCP error
  (`isError`), so a client can no longer read a save that did not happen as
  a success. The text is the same `{"error": ...}` JSON as before.
- `workspace_job_status` now documents the `refused` status and the job's
  `refusal` field: an approved job the server refused to start (storage
  quota, a mount outside the allowed data roots) reads `refused` with the
  refusal code, instead of `starting`. A resubmit with the same
  `submission_key` retries it.

## [1.3.0] - 2026-09-30

### Added
- **Updating metadata records**
  - `update_metadata_record(canvas_id, collection_name, data_id, update_data)`
    changes fields on an existing record. Only the keys you send change, so
    filling in one column leaves the rest of the record as it was.
  - `update_metadata_records(canvas_id, collection_name, updates)` does the same
    for many records at once, such as setting a group on every sample in a
    dataset. Every row is checked before anything is written, so one bad row
    means nothing changes. After that, each record gets its own result, and one
    failure does not stop the rest.
  - Both refuse the keys that identify a record or control access to it
    (`_id`, `owner`, `perms`, `DID`), and both are writes: assistants should
    show the changes and ask before calling them.

- **Watching a run, and honest logs (Phase 4)**
  - `watch_run(run_id)` reports how far a run has actually got — steps done,
    which step is running now, which failed and with what exit code — instead of
    a single status word. It reads Nextflow's task trace, which is the only
    place that detail exists.
  - `get_run_log` no longer tells you to "try again shortly" for a run whose
    logs are gone. Finished runs whose directory has been cleaned up now say so
    plainly and point at what still works, instead of implying the logs are on
    their way.

- **Results and apps (Phase 3)** — what a finished run actually found, instead
  of a list of files:
  - `summarize_results(run_id)` reports the significant genes per comparison,
    the strongest movers each way, and an alignment/QC verdict. An RNA-seq run
    produces five parallel DESeq2 outputs, so it also reports where the
    quantifiers agree — on the reference run that is three genes found by all
    five. It reads only the small result tables, never the multi-megabyte HTML
    reports, and treats "no genes were significant" as a result rather than an
    error.
  - `list_results(run_id)` groups a run's outputs by what they are —
    differential expression, quality control, quantification, alignments —
    with readable sizes, hiding and counting intermediates.
  - `suggest_apps(run_id)` recommends which viewer to open results in (GSEA
    Explorer, Cellxgene, IGV), matching against the apps actually installed so
    it never points at one that is missing.
- **Getting data in, and pre-flight (Phase 2)** — the steps between "I have
  fastqs" and a correctly-configured run:
  - `preflight_run(run_id)` checks a run for the mistakes that waste cluster
    time *before* it launches: a sample dataset with no files, a design-file
    path that does not exist, sheet sample names that do not match the dataset
    (in both directions), groups without replicates, empty inputs. Every
    finding carries a plain-language fix. Live on staging it correctly passes
    healthy runs and flags a real example run whose dataset is missing and
    whose two design files point at paths that are gone.
  - `prepare_samples(name, files)` builds a sample dataset from a list of fastq
    paths in one call, pairing R1/R2 automatically across the conventions that
    occur in practice, and refusing to silently drop a file whose name
    collapses to another sample's.
  - `make_sample_sheet(run_id, groups, comparisons)` authors the
    experimental-design sheets and places them where the run expects them,
    refusing a group without replicates or a comparison naming an unknown
    group.
- **Guided discovery and launch (Phase 1)** — a scientist who states a *goal*
  instead of naming a pipeline is now guided all the way to launch:
  - `recommend_pipeline(goal)` matches plain-language goals to pipelines and
    explains why, understanding assay language the catalog never spells out
    (chromatin accessibility → ATAC-seq, SNPs → variant calling). It returns the
    most recent successful run to clone, and returns *nothing* rather than
    guessing when no pipeline plausibly fits.
  - `plan_run(pipeline_id)` reduces a pipeline to at most 8 answerable
    decisions with plain-language labels, hiding the reference/index paths and
    process-option groups that make up the bulk of its inputs (live: 70 inputs
    and 61 option groups on RNA-seq become 8 questions). It states what it
    withheld rather than dropping it silently.
  - `list_featured_pipelines()` exposes the admin-curated catalog as the
    starting point for discovery, instead of the full process list.
  - `get_started()` returns the tool chain for the three journeys people arrive
    with: starting an analysis, diagnosing a failure, finding results.

- **Better failure diagnosis from chat** — `get_run_log` now returns the log
  that actually carries the failure. On real cluster (LSF) runs the backend
  returns no `.command.*` files and `err.log` is a fixed 42-byte job-starter
  stub, which the previous ordering handed back as "the most relevant log".
  Substantive logs (`log.txt`, `.nextflow.log`) now outrank near-empty stubs.
- Run-execution tools: `get_run_details`, `create_vmeta_dataset`, `duplicate_run`,
  `update_run`, `initiate_run` — enables duplicating and launching runs via MCP
  (previously runs were read-only). `update_run` enforces the `permission`+`groupId`
  requirement, rejects empty-string input values, and checks equal-length
  spreadsheet arrays.
- **OAuth Bearer token support** - Accept `Authorization: Bearer` credentials and emit `WWW-Authenticate` for OAuth discovery.
- **Chat-friendly run journey (Phase 0)** — `get_run_log` surfaces execution
  logs (`.command.err`/Nextflow tail) so failed runs are diagnosable from chat;
  `get_run` now returns a plain-language `status_display` + `next_steps`;
  `get_run_details` returns a compact summary by default (`verbose=True` for the
  full editable config); all use a shared `{summary, next_steps, data}` envelope.
  The compact summary carries the run's own name and a plain-text description
  (stored HTML stripped), keeps each sample input's `vmetaCollectionId` so
  samples can be re-pointed, and handles the dict-shaped `inputs` that external
  (nf-core/Nextflow) pipelines return instead of a list.

### Fixed
- **`update_process` no longer recreates every process port.** Each call used
  to delete and re-add every input and output port, even for a script-only
  edit, so every port got a new id and output ports lost their publish pattern.
  Ports are now matched to the stored ones by id, or else by display name, and
  updated in place. Only new ports are added, and only ports left out of a side
  you send are removed. Leave `inputParameters` or `outputParameters` out to
  keep that side as it is. Emptying a side needs `removeAllInputParameters` or
  `removeAllOutputParameters`. The result reports which ports were kept,
  updated, added and removed, and a refused port change sends nothing. Needs
  viafoundry_sdk 1.1.0, and the tool refuses to update with an older SDK.
- `list_runs` no longer advertises `pipelineId` as a sort field. The backend
  excludes it from its own whitelist, so any model that followed the tool
  contract got a hard 400; it is now aliased to `pipelineName`.

## [1.2.2] - 2026-03-06

### Added

- **`update_process`** - New tool to update an existing process with ownership guardrails to prevent cross-user modifications

### Changed

- **`remove_none` utility** - Extracted shared `remove_none` helper from `create_process` and `update_process` into `utils.py`
- **User identity endpoint** - Use correct `api/auth/v1/user` endpoint for resolving current user identity

## [1.2.1] - 2026-02-10

### Fixed

- **421 "Invalid Host header" behind reverse proxy** - Disabled MCP SDK DNS rebinding protection (`TransportSecuritySettings(enable_dns_rebinding_protection=False)`) which rejected non-localhost `Host` headers forwarded by Apache (`ProxyPreserveHost On`) and nginx. Auth is already handled by `CredentialsMiddleware`. See [python-sdk#1798](https://github.com/modelcontextprotocol/python-sdk/issues/1798).
- **Uvicorn access log showing proxy IP instead of real client IP** - Enabled `proxy_headers=True` and `forwarded_allow_ips="*"` in uvicorn so access logs resolve the real client IP from `X-Forwarded-For` instead of the Docker-internal proxy address (e.g. `10.99.0.1`).

## [1.2.0] - 2026-02-05

### Fixed

- **`duplicate_process`** - Removed unsupported `new_name` parameter to match SDK
- **`search_datasets`** - Changed to `dataset_id` and `filter_data` parameters (was `query`/`collection_id`)
- **`search_collections`** - Changed to `filter_data` parameter (was `query`)
- **`search_canvas`** - Changed to `filter_data` parameter (was `query`)
- **`search_metadata_fields`** - Changed to `filter_data` parameter (was `query`)
- **`search_metadata_records`** - Added required `canvas_id` and `collection_name` parameters
- **`create_collection`** - Now passes `collection_data` dict directly to SDK
- **`add_files_to_dataset`** - Changed to `file_data` dict (was `file_ids` list)
- **`create_metadata_record`** - Added `canvas_id`, `collection_name`, `data_entry` parameters
- **`get_metadata_record`** - Added `canvas_id`, `collection_name`, `data_id` parameters

### Changed

- **`upload_file`** - Now accepts base64-encoded content instead of local file path; `remote_dir` is now required
- **`download_file`** - Now returns base64-encoded content instead of saving to local filesystem

These changes enable file upload/download to work correctly when MCP server runs in Docker or remotely.

### Added

- **File size limit** - 200MB limit for upload/download with informative error messages
- **Request timeout** - 20s connect, 300s read timeout to prevent hanging
- **Path traversal protection** - Security validation with logging for upload file names
- **Enhanced error responses** - Detailed diagnostics including status code, reason, and response text
- **Route path validation** - Check for missing/empty routePath before download attempts

## [1.1.1] - 2026-01-30

### Changed

- **BREAKING: Renamed `filter_process_parameters` to `get_process_parameters`**
  - Aligns with SDK method rename from `filter_parameters` to `get_parameters`
  - Now calls `process.get_parameters(name=, qualifier=, fileType=, id_=)` directly

### Removed

- **Removed `get_pipeline_parameters` tool** - This tool was removed from the ViaFoundry SDK

### Fixed

- **Documentation: Added missing Run Management tools to README**
  - Added `list_runs` and `get_run` tools that were missing from documentation
  - Updated total tool count from 42 to 41

## [1.1.0] - 2025-12-08

### Changed

- **BREAKING: Removed STDIO server** - Simplified to HTTP-only architecture
  - Deleted `server.py` (STDIO transport)
  - Renamed `viafoundry-mcp-http` command to `viafoundry-mcp`
  - HTTP server is now the only transport method

### Improved

- **Code consistency** - All tool handlers now use `serialize_response()` utility
  - Better handling of nested Pydantic models
  - Protection against circular references
  - Consistent JSON serialization across all 42 tools

### Migration

If upgrading from 1.0.x:

- The `viafoundry-mcp-http` command is now just `viafoundry-mcp`
- STDIO transport is no longer supported
- HTTP configuration in `mcp.json` remains the same

## [1.0.1] - 2025-11-05

### Added

- **Major Expansion: 42 New Tools Across 4 Phases** (50 tools total):

  **Phase 1 - High-Impact Read Tools** (10 tools):
  - `get_process_revisions` - Get revision history for a specific process/pipeline
  - `list_process_parameters` - List all available parameters in ViaFoundry
  - `get_pipeline_parameters` - Get parameters for a specific pipeline by ID
  - `search_canvas` - Search for canvas visualizations in ViaFoundry
  - `get_canvas_details` - Get detailed information about a specific canvas
  - `search_metadata_fields` - Search for metadata field definitions
  - `get_collection_fields` - Get metadata fields for a specific collection
  - `search_metadata_records` - Search for metadata data records
  - `get_metadata_record` - Get a specific metadata record by ID
  - `get_all_report_paths` - Get all file paths for a specific report

  **Phase 2 - Workflow Enablers** (7 tools):
  - `duplicate_process` - Duplicate an existing process/pipeline
  - `filter_process_parameters` - Filter parameters by name, qualifier, file type, or ID
  - `add_files_to_dataset` - Add files to a dataset in a collection
  - `create_collection` - Create a new collection in the metadata system
  - `create_metadata_record` - Create a new metadata record in a collection
  - `get_field_details` - Get detailed information about a metadata field
  - `get_canvas_fields` - Get all fields associated with a canvas

  **Phase 3 - Advanced Management** (11 tools):
  - `create_process_config` - Create a process configuration
  - `create_process` - Create a new process/pipeline
  - `update_process` - Update an existing process/pipeline
  - `create_process_parameter` - Create a new process parameter
  - `update_process_parameter` - Update an existing process parameter
  - `create_canvas` - Create a new canvas visualization
  - `update_canvas` - Update an existing canvas
  - `create_metadata_field` - Create a new metadata field
  - `update_metadata_field` - Update an existing metadata field
  - `update_collection` - Update a collection
  - `update_metadata_record` - Update a metadata record

  **Phase 4 - Complete Coverage** (14 tools):
  - `delete_process` - Delete a process/pipeline
  - `delete_process_parameter` - Delete a process parameter
  - `delete_collection` - Delete a collection
  - `delete_canvas` - Delete a canvas
  - `delete_metadata_field` - Delete a metadata field
  - `delete_metadata_record` - Delete a metadata record
  - `create_menu_group` - Create a new menu group
  - `list_menu_groups` - List all menu groups
  - `update_menu_group` - Update a menu group
  - `get_menu_group_by_name` - Get menu group by name

### Fixed

- **SDK Method Call Corrections**:
  - Fixed menu group methods to use `process` module (not `metadata`)
  - Fixed parameter method names: `filter_parameters`, `create_parameter`, `update_parameter`, `delete_parameter`
  - Removed 2 non-existent tools: `collect_report_files`, `process_parameter`
  - All 50 tools now verified against ViaFoundry SDK

### Improved

- **Tool Count**: Expanded from 12 to 50 tools
- **Test Coverage**: Updated integration tests with 50-tool verification
- **Code Quality**: All SDK method calls verified and corrected

### Summary

This major release brings comprehensive ViaFoundry functionality to MCP:

- **CRUD Operations**: Full create, read, update, delete support for processes, metadata, and canvases
- **Workflow Management**: Tools for duplicating processes, managing parameters, and organizing data
- **Menu Groups**: Complete menu group management (create, list, update, get by name)
- **Data Management**: Dataset creation, file management, and metadata operations
- **Quality Assurance**: All tools verified against SDK, all tests passing (11/11)

**Tool Breakdown by Module**:

- Reports: 9 tools
- Process: 19 tools
- Metadata: 22 tools

**Total: 50 verified, working tools**

## [1.0.0] - 2025-11-04

### Added

- **Process/Pipeline Management Tools** (2 new tools):
  - `list_all_processes` - List all processes/pipelines in ViaFoundry
  - `get_process_details` - Get detailed information about a specific process by ID

- **Metadata & Dataset Search Tools** (3 new tools):
  - `search_datasets` - Search for dataset files in ViaFoundry metadata system
  - `search_collections` - Search for collections in ViaFoundry metadata system
  - `get_collection_details` - Get detailed information about a specific collection by ID

- **Interactive Setup**:
  - `viafoundry-mcp-setup` command for easy credential configuration
  - Interactive prompts when credentials are missing
  - Automatic .env file creation in recommended location

- **Test Suite**:
  - Unit tests for configuration management
  - Unit tests for client management
  - Integration tests for MCP tools
  - 11 test cases with pytest

### Changed

- **Code Reorganization**:
  - Renamed `mcp_server/` to `src/viafoundry_mcp/`
  - Split code into modular files: `server.py`, `client.py`, `config.py`
  - Created proper `test/` directory structure
  - Better separation of concerns

- **Documentation**:
  - Completely rewritten README with user-centric approach
  - Step-by-step IDE setup for Cursor, Claude Code, VSCode
  - Comprehensive troubleshooting section
  - Real-world example conversations

- **Tools**:
  - Expanded from 7 tools to 12 tools total
  - Categorized tools by function (Reports, Processes, Metadata)

### Summary

This release completes the ViaFoundry MCP implementation with:

- Full ViaFoundry API coverage (12 tools)
- Interactive credential management
- Modular, maintainable codebase
- Comprehensive test suite
- User-friendly documentation

## [0.1.0] - 2025-11-03

### Added

- Initial release of ViaFoundry MCP Server
- 7 MCP tools for ViaFoundry interaction:
  - fetch_report
  - list_processes
  - list_files
  - download_file
  - load_file
  - upload_file
  - get_report_dirs
- Automatic .env file detection from multiple locations
- Support for installation from GitHub
- UV package manager compatibility
- Comprehensive documentation (README, QUICKSTART, README-MCP)

### Changed

- **BREAKING**: Removed bundled viafoundry-sdk folder
  - SDK now installed automatically from PyPI
  - Cleaner package structure
  - Easier maintenance and updates

- **IMPROVED**: Simplified IDE configuration
  - No longer need to pass credentials in IDE config
  - Credentials automatically loaded from .env file
  - Multiple .env file locations supported:
    1. `~/.config/viafoundry-mcp/.env` (recommended)
    2. `mcp_server/.env` (development)
    3. `~/.viafoundry-mcp.env` (legacy)

### Fixed

- Environment variable loading from various working directories
- Package installation compatibility issues

### Security

- Credentials stored only in .env files (git-ignored)
- No credentials in configuration files
- Bearer token authentication with ViaFoundry

## Installation

### From GitHub

```bash
pip install git+https://github.com/viascientific/viafoundry-mcp.git
```

### From PyPI (Coming Soon)

```bash
pip install viafoundry-mcp
```

### Development

```bash
git clone https://github.com/viascientific/viafoundry-mcp.git
cd viafoundry-mcp
pip install -e .
```

## Migration Guide

### Upgrading from Pre-release

If you had the bundled SDK version:

1. **Remove old installation**:

   ```bash
   pip uninstall viafoundry-mcp viafoundry_sdk
   ```

2. **Install new version**:

   ```bash
   pip install git+https://github.com/viascientific/viafoundry-mcp.git
   ```

3. **Update IDE configuration** (simplified):

   ```json
   {
     "viafoundry": {
       "command": "viafoundry-mcp"
     }
   }
   ```

4. **Move credentials** (optional, for cleaner setup):
   ```bash
   mkdir -p ~/.config/viafoundry-mcp
   mv mcp_server/.env ~/.config/viafoundry-mcp/.env
   ```

## What Changed?

### Before (Complex Configuration)

```json
{
  "viafoundry": {
    "command": "viafoundry-mcp",
    "env": {
      "VIAFOUNDRY_HOSTNAME": "https://...",
      "VIAFOUNDRY_USERNAME": "...",
      "VIAFOUNDRY_PASSWORD": "..."
    }
  }
}
```

### After (Simple Configuration)

```json
{
  "viafoundry": {
    "command": "viafoundry-mcp"
  }
}
```

Credentials are now automatically loaded from your .env file!

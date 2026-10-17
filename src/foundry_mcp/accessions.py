"""Resolve public sequencing accessions to run accessions and a dataset plan.

Accepted: GEO series and samples (GSE, GSM), ArrayExpress (E-MTAB and other
E-XXXX-n studies), and anything the ENA portal filereport takes for read runs
(studies SRP/ERP/DRP and PRJxx, experiments SRX/ERX/DRX, runs SRR/ERR/DRR,
samples SAMN/SAMEA/SAMD and SRS/ERS/DRS).

How each reaches its runs, checked against live read-only calls:
  * ENA types go straight to the ENA portal filereport (result=read_run).
  * GSE and GSM go through NCBI eutils (db=gds): the esummary record's
    `extrelations` names the SRA study (SRP, for a series) or experiment (SRX,
    for a sample), with the series' BioProject as a fallback. This is the
    same NCBI route the web GEO import takes.
  * ArrayExpress goes through the BioStudies API, whose study links of type
    ENA name the ENA study.

Nothing is guessed: an accession that cannot be resolved is reported with the
reason, and a run whose layout ENA does not state is left out and reported.

The dataset records follow the web GEO import's shape, which production
uses: {name, geo_id, file_layout} with ONE run accession per record. The
initial run step downloads only the first id of a GEO record, and the dataset
refuses two records with the same name, so a sample with several runs becomes
one record per run.
"""
import json
import re
import time

import requests

ENA_FILEREPORT = "https://www.ebi.ac.uk/ena/portal/api/filereport"
EUTILS_ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EUTILS_ESUMMARY = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
BIOSTUDIES_STUDIES = "https://www.ebi.ac.uk/biostudies/api/v1/studies"

ENA_FIELDS = ",".join([
    "run_accession", "sample_accession", "sample_alias", "sample_title",
    "experiment_accession", "library_layout", "library_strategy",
    "instrument_platform", "fastq_bytes", "study_accession",
])

_TIMEOUT = 30
_RETRIES = 3
# NCBI allows three requests a second without an API key.
_NCBI_GAP_SECONDS = 0.34

_PATTERNS = [
    ("geo_series", re.compile(r"^GSE\d+$")),
    ("geo_sample", re.compile(r"^GSM\d+$")),
    ("arrayexpress", re.compile(r"^E-[A-Z]{4}-\d+$")),
    # The accession types ENA's filereport names for read_run.
    ("ena", re.compile(
        r"^(?:[ESDR]RP\d{6,}|PRJ[A-Z]{2}\d+|[ESDR]RX\d{6,}|[ESDR]RR\d{6,}"
        r"|SAMEA?\d{6,}|SAM[ND]\d{8}|[EDSR]RS\d{6,})$")),
]

_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def classify(accession):
    """The kind of an accession, or None when it is not one this tool takes."""
    acc = (accession or "").strip().upper()
    for kind, pattern in _PATTERNS:
        if pattern.match(acc):
            return kind
    return None


def _http_get(url, params=None):
    """GET with retries on rate limits and server errors. Returns (status, text)."""
    last = None
    for attempt in range(_RETRIES):
        try:
            resp = requests.get(url, params=params, timeout=_TIMEOUT)
        except requests.RequestException as e:
            last = e
        else:
            if resp.status_code != 429 and resp.status_code < 500:
                return resp.status_code, resp.text
            last = RuntimeError(f"HTTP {resp.status_code} from {url}")
        time.sleep(1 + attempt)
    raise last


def _pause():
    time.sleep(_NCBI_GAP_SECONDS)


class Unresolved(Exception):
    """An accession that cannot be turned into runs; the message says why."""


def _json(status, text, what):
    try:
        return json.loads(text) if text and text.strip() else None
    except ValueError:
        raise Unresolved(f"{what} answered HTTP {status} with a body that is not JSON.")


def ena_runs(accession):
    """ENA read runs for an accession ENA's filereport takes."""
    status, text = _http_get(ENA_FILEREPORT, {
        "accession": accession, "result": "read_run", "fields": ENA_FIELDS,
        "format": "json", "limit": "0",
    })
    body = _json(status, text, "ENA")
    if status != 200:
        message = body.get("message") if isinstance(body, dict) else None
        raise Unresolved(f"ENA refused {accession} (HTTP {status}): {message or 'no message'}")
    rows = body if isinstance(body, list) else []
    if not rows:
        raise Unresolved(f"ENA lists no public read runs for {accession}.")
    return rows


def _geo_record(accession, entry_type):
    _pause()
    status, text = _http_get(EUTILS_ESEARCH, {
        "db": "gds", "retmode": "json", "tool": "foundry-connect",
        "term": f"{accession}[ACCN] AND {entry_type}[ETYP]",
    })
    ids = ((_json(status, text, "NCBI esearch") or {}).get("esearchresult") or {}).get("idlist") or []
    if status != 200 or not ids:
        raise Unresolved(f"GEO has no record for {accession}.")
    _pause()
    status, text = _http_get(EUTILS_ESUMMARY, {
        "db": "gds", "retmode": "json", "tool": "foundry-connect", "id": ids[0],
    })
    result = (_json(status, text, "NCBI esummary") or {}).get("result") or {}
    record = result.get(ids[0]) or {}
    if (record.get("accession") or "").upper() != accession:
        raise Unresolved(f"GEO has no record for {accession}.")
    return record


def geo_targets(accession, kind):
    """The SRA accessions a GEO series or sample points to."""
    record = _geo_record(accession, "gse" if kind == "geo_series" else "gsm")
    targets = [rel.get("targetobject") for rel in record.get("extrelations") or []
               if (rel.get("relationtype") or "").upper() == "SRA" and rel.get("targetobject")]
    if not targets and kind == "geo_series" and record.get("bioproject"):
        targets = [record["bioproject"]]
    if not targets:
        raise Unresolved(
            f"{accession} has no SRA sequencing runs linked (array data, or a SuperSeries: "
            f"pass its SubSeries instead).")
    return targets


def _walk_links(node, found):
    if isinstance(node, list):
        for item in node:
            _walk_links(item, found)
    elif isinstance(node, dict):
        attrs = node.get("attributes") or []
        is_ena = any((a.get("name") or "").lower() == "type" and (a.get("value") or "").upper() == "ENA"
                     for a in attrs if isinstance(a, dict))
        url = (node.get("url") or "").strip()
        if is_ena and classify(url) == "ena" and url not in found:
            found.append(url)
        for key in ("links", "section", "subsections"):
            if key in node:
                _walk_links(node[key], found)


def arrayexpress_targets(accession):
    """The ENA accessions an ArrayExpress study links to in BioStudies."""
    status, text = _http_get(f"{BIOSTUDIES_STUDIES}/{accession}")
    if status == 404:
        raise Unresolved(f"BioStudies has no study {accession}.")
    body = _json(status, text, "BioStudies")
    if status != 200 or not isinstance(body, dict):
        raise Unresolved(f"BioStudies answered HTTP {status} for {accession}.")
    found = []
    _walk_links(body.get("section") or {}, found)
    if not found:
        raise Unresolved(f"{accession} has no ENA sequencing data linked in BioStudies.")
    return found


def resolve(accession):
    """ENA run rows for one accession. Raises Unresolved with the reason."""
    acc = (accession or "").strip().upper()
    kind = classify(acc)
    if kind is None:
        raise Unresolved(
            f"'{accession}' is not an accession this tool takes (GSE, GSM, SRP/ERP/DRP, PRJxx, "
            f"SRX/ERX, SRR/ERR/DRR, SAMN/SAMEA, E-MTAB).")
    if kind == "ena":
        return ena_runs(acc)
    targets = geo_targets(acc, kind) if kind.startswith("geo") else arrayexpress_targets(acc)
    rows = []
    for target in targets:
        rows.extend(ena_runs(target))
    return rows


def _safe_name(text):
    return _UNSAFE_NAME.sub("_", (text or "").strip()).strip("_")


def _layout(value):
    return {"PAIRED": "pair", "SINGLE": "single"}.get((value or "").strip().upper())


def _fastq_sizes(value):
    parts = [p for p in (value or "").split(";") if p.strip()]
    sizes = []
    for part in parts:
        try:
            sizes.append(int(part))
        except ValueError:
            sizes.append(0)
    return sizes


def build_plan(accessions):
    """Resolve accessions and group their runs by sample into dataset records."""
    unresolved, warnings, runs, seen = [], [], [], set()
    for accession in accessions:
        try:
            rows = resolve(accession)
        except Unresolved as e:
            unresolved.append({"accession": accession.strip(), "reason": str(e)})
            continue
        except Exception as e:  # network trouble stays with the accession that hit it
            unresolved.append({"accession": accession.strip(),
                               "reason": f"could not reach the archive: {e}"})
            continue
        for row in rows:
            run = (row.get("run_accession") or "").strip()
            if not run or run in seen:
                continue
            if _layout(row.get("library_layout")) is None:
                unresolved.append({"accession": run, "reason": (
                    f"ENA does not state a paired or single layout for {run} "
                    f"(library_layout '{row.get('library_layout') or ''}').")})
                seen.add(run)
                continue
            seen.add(run)
            runs.append(row)

    samples, order = {}, []
    for row in runs:
        key = row.get("sample_accession") or row.get("experiment_accession") or row["run_accession"]
        if key not in samples:
            samples[key] = {"rows": [], "sample_accession": row.get("sample_accession") or "",
                            "sample_alias": row.get("sample_alias") or "",
                            "title": row.get("sample_title") or ""}
            order.append(key)
        samples[key]["rows"].append(row)

    base_names = {}
    for key in order:
        s = samples[key]
        base_names[key] = (_safe_name(s["title"]) or _safe_name(s["sample_alias"])
                           or _safe_name(s["sample_accession"]) or _safe_name(key))
    counts = {}
    for name in base_names.values():
        counts[name] = counts.get(name, 0) + 1

    sample_out, records, total = [], [], 0
    used_names = set()
    for key in order:
        s = samples[key]
        name = base_names[key]
        if counts[name] > 1:
            name = f"{name}_{_safe_name(s['sample_alias']) or _safe_name(key)}"
        layouts = sorted({_layout(r.get("library_layout")) for r in s["rows"]})
        layout = layouts[0] if len(layouts) == 1 else "mixed"
        if layout == "mixed":
            warnings.append(f"Sample {name} mixes paired and single runs; check before running.")
        if len(s["rows"]) > 1:
            warnings.append(
                f"Sample {name} has {len(s['rows'])} runs; each run is its own record "
                f"(a GEO record downloads one run), so combine them after the run if they "
                f"are technical replicates.")
        raw = 0
        for r in s["rows"]:
            sizes = _fastq_sizes(r.get("fastq_bytes"))
            raw += sum(sizes)
            strategy = (r.get("library_strategy") or "").strip()
            if strategy and "RNA" not in strategy.upper():
                warnings.append(f"Run {r['run_accession']} ({name}) is {strategy}, not RNA sequencing.")
            if not sizes:
                warnings.append(f"Run {r['run_accession']} ({name}) has no fastq size in ENA.")
            elif _layout(r.get("library_layout")) == "pair" and len(sizes) == 1:
                warnings.append(
                    f"Run {r['run_accession']} ({name}) is labeled paired but ENA lists one fastq "
                    f"file; the download may not split into two mates.")
            record_name = name if len(s["rows"]) == 1 else f"{name}_{r['run_accession']}"
            while record_name in used_names:
                record_name = f"{record_name}_{r['run_accession']}"
            used_names.add(record_name)
            records.append({"name": record_name, "geo_id": r["run_accession"],
                            "file_layout": _layout(r.get("library_layout"))})
        total += raw
        sample_out.append({
            "name": name, "sample_accession": s["sample_accession"],
            "sample_alias": s["sample_alias"], "layout": layout,
            "runs": [r["run_accession"] for r in s["rows"]], "raw_bytes": raw,
        })

    return {
        "sample_count": len(sample_out), "run_count": len(records),
        "total_raw_bytes": total, "samples": sample_out, "records": records,
        "unresolved": unresolved, "warnings": warnings,
    }

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

A long list is looked up in batches: runs and experiments (including the
experiments GEO samples point to) through the ENA portal search with
includeAccessions, one accession type per call, and GEO accessions through
one OR-joined esearch and one esummary per batch. A batch answers only what
it found, so each accession it does not return is reported as unresolved.

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
ENA_SEARCH = "https://www.ebi.ac.uk/ena/portal/api/search"
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
# Accessions per call: an ENA search answers 200 runs in under a second, and
# 100 OR-joined GEO accessions keep the eutils URL short.
_ENA_BATCH = 200
_GEO_BATCH = 100

# ENA's search takes a list of one accession type per call (a mixed list is
# refused), and each row names its run and experiment, so a row is traced back
# to the accession asked for by this field.
_ENA_BATCH_FIELDS = [
    (re.compile(r"^[ESDR]RR\d{6,}$"), "run_accession"),
    (re.compile(r"^[ESDR]RX\d{6,}$"), "experiment_accession"),
]

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
    return _http_send(requests.get, url, params=params)


def _http_post(url, data=None):
    """POST a form with the same retries; ENA takes a long accession list this way."""
    return _http_send(requests.post, url, data=data)


def _http_send(send, url, **kwargs):
    last = None
    for attempt in range(_RETRIES):
        try:
            resp = send(url, timeout=_TIMEOUT, **kwargs)
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


def _chunks(items, size):
    return [items[i:i + size] for i in range(0, len(items), size)]


def _geo_chunk(chunk, entry_type):
    """One esearch and one esummary for up to _GEO_BATCH GEO accessions of one type."""
    terms = " OR ".join(f"{acc}[ACCN]" for acc in chunk)
    if len(chunk) > 1:
        terms = f"({terms})"
    _pause()
    status, text = _http_get(EUTILS_ESEARCH, {
        "db": "gds", "retmode": "json", "tool": "foundry-connect",
        "retmax": str(len(chunk) * 10), "term": f"{terms} AND {entry_type}[ETYP]",
    })
    ids = ((_json(status, text, "NCBI esearch") or {}).get("esearchresult") or {}).get("idlist") or []
    records = {}
    if status == 200 and ids:
        _pause()
        status, text = _http_get(EUTILS_ESUMMARY, {
            "db": "gds", "retmode": "json", "tool": "foundry-connect", "id": ",".join(ids),
        })
        result = (_json(status, text, "NCBI esummary") or {}).get("result") or {}
        for uid in ids:
            record = result.get(uid) or {}
            acc = (record.get("accession") or "").upper()
            if acc in chunk:
                records[acc] = record
    return {acc: records.get(acc) or Unresolved(f"GEO has no record for {acc}.") for acc in chunk}


def _geo_records(accs, entry_type):
    """The esummary record, or the error, for each GEO series or sample."""
    found = {}
    for chunk in _chunks(accs, _GEO_BATCH):
        try:
            found.update(_geo_chunk(chunk, entry_type))
        except Exception as e:  # network trouble stays with the accessions that hit it
            found.update({acc: e for acc in chunk})
    return found


def _sra_targets(accession, kind, record):
    """The SRA accessions a GEO series or sample record points to."""
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


def _ena_runs_or_error(accession):
    try:
        return ena_runs(accession)
    except Exception as e:
        return e


def _ena_batch(accs, field, found):
    """Fill found[acc] with each accession's ENA rows, or the error, one search per chunk."""
    for chunk in _chunks(accs, _ENA_BATCH):
        try:
            status, text = _http_post(ENA_SEARCH, {
                "result": "read_run", "includeAccessions": ",".join(chunk),
                "fields": ENA_FIELDS, "format": "json", "limit": "0",
            })
            body = _json(status, text, "ENA")
        except Exception as e:
            found.update({acc: e for acc in chunk})
            continue
        if status != 200:
            # One accession ENA refuses fails the whole list, so ask for each
            # alone and only that one is reported, with ENA's message.
            found.update({acc: _ena_runs_or_error(acc) for acc in chunk})
            continue
        rows = {acc: [] for acc in chunk}
        for row in body if isinstance(body, list) else []:
            asked = (row.get(field) or "").strip().upper()
            if asked in rows:
                rows[asked].append(row)
        found.update({acc: got or Unresolved(f"ENA lists no public read runs for {acc}.")
                      for acc, got in rows.items()})


def _batch_field(accession):
    for pattern, field in _ENA_BATCH_FIELDS:
        if pattern.match(accession):
            return field
    return None


def resolve_all(accessions):
    """ENA run rows for each accession, in the order given: (accession, rows or error).

    Runs and experiments (including the experiments GEO samples point to) are
    looked up in ENA in batches, and GEO series and samples in eutils in
    batches, so a long list costs a few calls rather than several per
    accession. Studies and samples go one by one: each answers many runs.
    """
    targets, geo = {}, {"geo_series": [], "geo_sample": []}
    for accession in accessions:
        acc = (accession or "").strip().upper()
        if acc in targets:
            continue
        kind = classify(acc)
        if kind is None:
            targets[acc] = Unresolved(
                f"'{accession}' is not an accession this tool takes (GSE, GSM, SRP/ERP/DRP, PRJxx, "
                f"SRX/ERX, SRR/ERR/DRR, SAMN/SAMEA, E-MTAB).")
        elif kind == "ena":
            targets[acc] = [acc]
        elif kind == "arrayexpress":
            try:
                targets[acc] = arrayexpress_targets(acc)
            except Exception as e:
                targets[acc] = e
        else:
            geo[kind].append(acc)
            targets[acc] = None
    for kind, accs in geo.items():
        records = _geo_records(accs, "gse" if kind == "geo_series" else "gsm")
        for acc in accs:
            record = records[acc]
            try:
                targets[acc] = record if isinstance(record, Exception) else _sra_targets(acc, kind, record)
            except Unresolved as e:
                targets[acc] = e

    wanted = []
    for t in targets.values():
        if isinstance(t, list):
            wanted.extend(x.upper() for x in t if x.upper() not in wanted)
    found = {}
    for _, field in _ENA_BATCH_FIELDS:
        _ena_batch([t for t in wanted if _batch_field(t) == field], field, found)
    for target in wanted:
        if target not in found:
            found[target] = _ena_runs_or_error(target)

    out = []
    for accession in accessions:
        t = targets[(accession or "").strip().upper()]
        if isinstance(t, Exception):
            out.append((accession, t))
            continue
        rows = []
        for target in t:
            got = found[target.upper()]
            if isinstance(got, Exception):
                rows = got
                break
            rows.extend(got)
        out.append((accession, rows))
    return out


# Studies and projects expand to runs only inside ENA, so they cannot be cut
# into sample pages before the lookup that a page is meant to keep small.
_STUDY_LEVEL = re.compile(r"^(?:[ESDR]RP\d{6,}|PRJ[A-Z]{2}\d+)$")


def _gsm_number(acc):
    return int(acc[3:])


def sample_units(accessions):
    """Flatten accessions into one ordered list of sample level accessions.

    Returns (units, unresolved). A GEO series becomes its GSMs sorted by
    accession number, so the order is the same on every call; every other
    accession stays one unit, in the order given. A repeated unit is kept once.
    Raises ValueError for study, project and ArrayExpress accessions, which
    cannot be split before lookup, and when a series cannot be expanded while
    other units remain: dropping its GSMs would shift total_samples and every
    later offset between calls. When nothing expands at all, units is empty
    and unresolved says why.
    """
    cleaned = [(a or "").strip().upper() for a in accessions]
    refused = [a for a in cleaned if classify(a) == "arrayexpress" or _STUDY_LEVEL.match(a)]
    if refused:
        raise ValueError(
            f"max_samples and offset cannot page {', '.join(refused)}: study, project and "
            f"ArrayExpress accessions resolve to their runs in one lookup. Pass the GEO "
            f"series (GSE) or the sample, experiment or run accessions instead, or call "
            f"without max_samples and offset.")
    series = [a for a in dict.fromkeys(cleaned) if classify(a) == "geo_series"]
    records = _geo_records(series, "gse") if series else {}
    units, unresolved, seen = [], [], set()
    for acc in cleaned:
        if classify(acc) != "geo_series":
            members = [acc]
        else:
            record, members = records[acc], []
            try:
                if isinstance(record, Exception):
                    raise record
                # The series level check the unpaged call makes, so an array
                # series or a SuperSeries is refused with the same reason.
                _sra_targets(acc, "geo_series", record)
                members = sorted({(s.get("accession") or "").strip().upper()
                                  for s in record.get("samples") or []
                                  if classify(s.get("accession")) == "geo_sample"}, key=_gsm_number)
                if not members:
                    raise Unresolved(f"GEO lists no samples for {acc}.")
            except Exception as e:
                if acc not in seen:
                    reason = str(e) if isinstance(e, Unresolved) else f"could not reach the archive: {e}"
                    unresolved.append({"accession": acc, "reason": reason})
                    seen.add(acc)
                continue
        for unit in members:
            if unit not in seen:
                seen.add(unit)
                units.append(unit)
    if unresolved and units:
        failed = "; ".join(f"{u['accession']} ({u['reason']})" for u in unresolved)
        raise ValueError(
            f"Cannot page these accessions: a series could not be expanded into its samples, "
            f"so the page boundaries would shift between calls. {failed}. Retry, or leave "
            f"that series out.")
    return units, unresolved


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
    for accession, rows in resolve_all(accessions):
        if isinstance(rows, Unresolved):
            unresolved.append({"accession": accession.strip(), "reason": str(rows)})
            continue
        if isinstance(rows, Exception):  # network trouble stays with the accession that hit it
            unresolved.append({"accession": accession.strip(),
                               "reason": f"could not reach the archive: {rows}"})
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

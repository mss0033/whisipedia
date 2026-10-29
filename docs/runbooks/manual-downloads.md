# Downloading by hand what the library may not crawl

Some hosts disallow crawlers in their robots.txt, and some answer a plain client with a bot wall.
The library honours both strictly (owner decision 2026-10-21): it never fetches from them, not even
through another host's redirect. A person may still download their public files with a browser, and
the owner does, about ten files a day per source (owner request 2026-10-24). The library makes no
request of those hosts at any step: its lists come from hosts that allow it, from file-name patterns,
from curated lists, or from listing pages the owner saved.

## Every day

1. Open `Z:\whisipedia\_incoming\_manual\TODAY.html` (also `TODAY.txt`). It lists, per source, up
   to ten links and the exact name to save each one under. Links offered before and not received
   come first. A listing page that is needed first is listed as such ("save the page into `_listing`
   as ...": Ctrl+S, "Webpage, HTML only").
2. Open each link and save the file into that source's folder,
   `Z:\whisipedia\_incoming\_manual\<source>\`, under the name shown. For a landing page ("open the
   page and save its document"), save the PDF it offers under the name shown.
3. Nothing else. Within the hour, `whisipedia-manual.timer` takes the files in: each is checked (an
   archive must read whole, with every zip, gzip and bzip2 CRC; a PDF must be one; an HTML page saved
   in a file's place is refused), screened like any download, and ingested. A file that fails its
   check is renamed `<name>.bad` and offered again the next day, with the reason on the page.

`wsp collections manual --status` shows each source's progress. `--sync --today` writes the page
again now; `--sync --rebuild` remakes the lists.

## How each list is made

`access.manual` in the registry (wsp_cli.manual):

| kind | the list comes from | used by |
|---|---|---|
| `pubmed` | the update files after the newest sealed `pubmedYY-uNNNN` generation | `pubmed` |
| `saved` | the source's own discovery, reading listing pages the owner saved into `_listing/` | `pubchem`, `army-pubs-public` |
| `discovery` | the source's own discovery, on hosts that allow us | `wbdg-criteria` (wbdg.org), `geonames`, `gebco-grid` |
| `list` | a curated list in the registry | curated sources |
| `kiwix-checksums` | every newer ZIM the official mirrors list for a Kiwix source: Kiwix's own `.sha256` of it | `kiwix-checksums` |

`first` puts the most valuable items first, for example PubChem's chemical safety summaries
(`CID-LCSS.xml.gz`) and WBDG's Unified Facilities Criteria. `again_days` offers a file again when its
issuer refreshes it: GeoNames every 90 days, PubChem and GEBCO yearly.

A document collection's received files go through `wsp collections fetch` with them standing for the
listing (`kind: candidates`, a sample, so nothing held is inferred withdrawn). PubMed's become a
generation of the new update files, `pubmedYY-uNNNN`, in an unbroken run of numbers. A gap waits for
the missing file.

## Kiwix checksums

Kiwix's own hosts refuse robots since October 2026 (`download.kiwix.org` and the catalog's
`opds.library.kiwix.org`: `Disallow: /`). The library lists and downloads ZIMs from the official
mirrors (`ftp.fau.de/kiwix`, which has every directory, and `dumps.wikimedia.org/kiwix` for the
Wikimedia projects), and takes a file only when Kiwix's own SHA-256 of it is known (owner decision
2026-10-26):

* for the files the library already holds, from the vault manifests, recorded when each was downloaded
  from Kiwix (copied to `<state>/kiwix-checksums/` by the sync);
* for a newer file, from the `.sha256` the owner saves into `_manual/kiwix-checksums/`: the daily page
  links `https://download.kiwix.org/zim/<dir>/<file>.zim.sha256` and names the file to save.

Until its checksum is saved, a newer ZIM waits and the source keeps the version it can verify. A
saved file that is not that ZIM's checksum (an error page, another file's) is set aside as `*.bad`.

## Limits

* NCBI's MD5 sidecars are not downloaded by hand: a PubMed or PubChem file is checked by its gzip
  CRCs, and its SHA-256 recorded on arrival checks the copy into the vault, not the origin.
* No browser extension or script may download the lists: that would be an automated client, which
  is what these hosts refuse. The page is for a person.

## The older one-shot form

`wsp collections manual SOURCE` prints a fixed list's links and folder, and `--import` takes them in.
It is kept for GeoNames and GEBCO, which also take part in the daily page.

## A download kept without a check

A file the library did download but could not verify (no checksum upstream and no second download,
kept as `<file>.uncorroborated`) can be taken as it is on the owner's word:
`wsp collections fetch --source SOURCE --version CYCLE --adopt-kept KEY` (a zip must read whole).

## Permission requests (owner request 2026-10-28)

Some sources need the publisher's written permission (copyright: the Stanford Encyclopedia of
Philosophy, Hesperian, Sphere, UK MOD doctrine, the NCHFP's own pages), and some hosts keep the
library out with robots.txt (crawl: Kiwix, NCBI, WBDG, UniProt, COD, ICRC, Lovdata, NRC, OSHA). An
allowance from a crawl host also ends the manual downloads for its sources.

The requests are listed in `tools/permissions/requests.json`. The hourly sync copies it and
`tools/permissions/permissions.py` into `Z:\whisipedia\_incoming\_manual\_permissions\` and runs the
script there. It writes `PERMISSIONS.html`, and in each request's folder a `letter.txt`, a `draft.eml`
(double-click: an unsent draft, To and Subject filled in) and `mark-sent.cmd`.

1. Open `PERMISSIONS.html`, press **Write the e-mail** (or open `draft.eml`; or, where there is no
   address, **Contact page** and paste the letter), put your name where it says `[your name]`, send.
2. Double-click `mark-sent.cmd` in that request's folder.
3. When the answer comes, save it into the same folder as `reply.eml` (or paste it into `reply.txt`).
   The library raises an alert; Claude reads it, records the permission (or refusal) in the registry,
   and writes `granted.txt` or `refused.txt` there.

After 21 days without an answer the page offers a short follow-up letter and an alert reminds you.
The library never sends mail and never writes your name or address into a letter.

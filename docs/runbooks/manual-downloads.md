# Downloading by hand what the library may not crawl

Some hosts disallow crawlers in their robots.txt, and the library honours that strictly (owner
decision 2026-10-21): it never fetches from them, not even through another host's redirect. A
person may still download their public files with a browser. For a source that is a short, fixed
list of files, that takes three steps, and the library then makes no request of its own.

## The three steps

1. In a WSL terminal: `wsp collections manual geonames`. It prints the links to download and the
   folder to save them in (a Windows path, `\\192.168.1.169\Bellerophon\whisipedia\_incoming\_manual\geonames`,
   which a browser's Save dialog takes as it is).
2. Open each link in the browser and save the file into that folder, keeping its name.
3. `wsp collections manual geonames --import`. Each file is checked (a zip must read whole: every
   member's CRC), screened like any download, planned, and ingested; the folder is emptied as files
   are taken. A file still missing is named with its link: download it and run step 3 again.

Nothing is fetched by the library in step 3 (its fetcher refuses every request).

## Sources

| Source | Why by hand | Files |
|---|---|---|
| `geonames` | download.geonames.org: robots.txt `Disallow: /` | 9 (about 1 GB): the gazetteer, alternate names, hierarchy, admin and country codes, readme |
| `gebco-grid` | BODC's link redirects to dap.ceda.ac.uk: robots.txt `Disallow: /` | 1 (7.5 GB, the yearly grid; 2024 is in the library) |

Not offered: sources whose documents are many (Army publications, about 30 GB of separate PDFs;
WBDG's criteria, hundreds of PDFs from a disallowed files bucket), and the DSpace repositories (WHO
IRIS, NPS Calhoun, FAO), whose robots.txt disallows only their API: those are to be read through
their sitemaps instead. `wsp collections manual` refuses a source that is not a fixed list of files.

## A download kept without a check

A file the library did download but could not verify (no checksum upstream and no second download,
kept as `<file>.uncorroborated`) can be taken as it is on the owner's word:
`wsp collections fetch --source SOURCE --version CYCLE --adopt-kept KEY` (a zip must read whole).

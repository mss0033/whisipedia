//! Merge the segments of a Tantivy index into fewer, larger ones.
//!
//! The Python binding (tantivy-py) exposes no merge, and Whisipedia's writer never merges on its own
//! during bulk loads, so the live index kept 30-odd segments: every query walks each segment's term
//! dictionary and posting lists. This tool packs segments into groups of at most `--target-docs`
//! documents (largest first, first fit) and merges each group, one at a time, then removes the files
//! nobody uses. Only postings, fast fields and stored fields are copied: no tokenizer is needed.
//!
//! Usage: tantivy-merge <index dir> [--target-docs N] [--dry-run]
//! Run it only through `wsp index merge`, which holds the machine's index-writer locks.

use std::process::ExitCode;
use std::time::Instant;

use tantivy::indexer::NoMergePolicy;
use tantivy::index::SegmentId;
use tantivy::{Index, IndexWriter, TantivyDocument};

fn plan(mut segments: Vec<(SegmentId, u32)>, target: u64) -> Vec<Vec<(SegmentId, u32)>> {
    segments.sort_by(|a, b| b.1.cmp(&a.1));
    let mut bins: Vec<(u64, Vec<(SegmentId, u32)>)> = Vec::new();
    for seg in segments {
        let n = seg.1 as u64;
        match bins.iter_mut().find(|(sum, _)| sum + n <= target) {
            Some((sum, members)) => {
                *sum += n;
                members.push(seg);
            }
            None => bins.push((n, vec![seg])),
        }
    }
    bins.into_iter().map(|(_, m)| m).collect()
}

fn run() -> tantivy::Result<bool> {
    let args: Vec<String> = std::env::args().collect();
    let dir = args.get(1).cloned().unwrap_or_default();
    if dir.is_empty() || dir.starts_with("--") {
        eprintln!("usage: tantivy-merge <index dir> [--target-docs N] [--dry-run]");
        return Ok(false);
    }
    let mut target: u64 = 35_000_000;
    let mut dry_run = false;
    let mut i = 2;
    while i < args.len() {
        match args[i].as_str() {
            "--target-docs" => {
                target = args.get(i + 1).and_then(|v| v.parse().ok()).unwrap_or(target);
                i += 1;
            }
            "--dry-run" => dry_run = true,
            other => {
                eprintln!("unknown argument: {other}");
                return Ok(false);
            }
        }
        i += 1;
    }
    let index = Index::open_in_dir(&dir)?;
    let metas = index.searchable_segment_metas()?;
    let docs_before: u64 = metas.iter().map(|m| m.num_docs() as u64).sum();
    let segments: Vec<(SegmentId, u32)> = metas.iter().map(|m| (m.id(), m.max_doc())).collect();
    let groups = plan(segments, target);
    let to_merge: Vec<&Vec<(SegmentId, u32)>> = groups.iter().filter(|g| g.len() > 1).collect();
    println!(
        "{} segment(s), {} documents; plan: {} segment(s) after {} merge(s) (target {} docs)",
        metas.len(),
        docs_before,
        groups.len(),
        to_merge.len(),
        target
    );
    for g in &to_merge {
        let sizes: Vec<String> = g.iter().map(|(_, n)| n.to_string()).collect();
        println!("  merge {} segments: {}", g.len(), sizes.join(" + "));
    }
    if dry_run || to_merge.is_empty() {
        return Ok(true);
    }
    // One thread, a small heap: merging streams; no documents are added.
    let mut writer: IndexWriter<TantivyDocument> = index.writer_with_num_threads(1, 50_000_000)?;
    writer.set_merge_policy(Box::new(NoMergePolicy));
    for (k, g) in to_merge.iter().enumerate() {
        let ids: Vec<SegmentId> = g.iter().map(|(id, _)| *id).collect();
        let started = Instant::now();
        let merged = writer.merge(&ids).wait()?;
        println!(
            "  merge {}/{} done in {:.0} s: {} documents",
            k + 1,
            to_merge.len(),
            started.elapsed().as_secs_f64(),
            merged.map(|m| m.num_docs()).unwrap_or(0)
        );
    }
    writer.wait_merging_threads()?;
    let index = Index::open_in_dir(&dir)?;
    let after = index.searchable_segment_metas()?;
    let docs_after: u64 = after.iter().map(|m| m.num_docs() as u64).sum();
    println!("{} segment(s), {} documents", after.len(), docs_after);
    if docs_after != docs_before {
        eprintln!("document count changed: {docs_before} -> {docs_after}");
        return Ok(false);
    }
    let gc_writer: IndexWriter<TantivyDocument> = index.writer_with_num_threads(1, 15_000_000)?;
    gc_writer.set_merge_policy(Box::new(NoMergePolicy));
    let removed = gc_writer.garbage_collect_files().wait()?;
    println!("removed {} unused file(s)", removed.deleted_files.len());
    Ok(true)
}

fn main() -> ExitCode {
    match run() {
        Ok(true) => ExitCode::SUCCESS,
        Ok(false) => ExitCode::from(2),
        Err(e) => {
            eprintln!("error: {e}");
            ExitCode::FAILURE
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ids(n: usize) -> Vec<SegmentId> {
        (0..n).map(|_| SegmentId::generate_random()).collect()
    }

    #[test]
    fn packs_largest_first_without_passing_the_target() {
        let id = ids(6);
        let segs = vec![(id[0], 58), (id[1], 27), (id[2], 6), (id[3], 26), (id[4], 8), (id[5], 1)];
        let groups = plan(segs, 35);
        let sizes: Vec<Vec<u32>> = groups.iter().map(|g| g.iter().map(|(_, n)| *n).collect()).collect();
        assert_eq!(sizes, vec![vec![58], vec![27, 8], vec![26, 6, 1]]);
    }
}

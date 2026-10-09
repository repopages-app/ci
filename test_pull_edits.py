"""Unit tests for `repopages_push.py pull-edits`. Stdlib only, no network: Confluence, gh and the
sync trigger are fakes; git runs against throw-away local repositories.

    python3 -I -m unittest -v
"""
import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import repopages_push as rp  # noqa: E402

SITE = "https://acme.atlassian.net"
CLOUD = "11111111-2222-3333-4444-555555555555"
MD = "# Support reply drafter\n\nAnswer in two sentences — kurz und freundlich.\n"


def attachment(md=MD, **over):
    h = {"repo": "owner/name", "path": "docs/prompts/drafter.md", "baseSha": "ab" * 20, "pageId": "123", "version": 17,
         "editor": "557058:abc", "editorName": "Ana Kovač", "at": "2026-10-09T08:15:00.000Z",
         "proposalHash": hashlib.sha256(md.encode("utf-8")).hexdigest()}
    h.update(over)
    h = {k: v for k, v in h.items() if v is not None}
    return f"<!-- repopages-pending {json.dumps(h, ensure_ascii=False)} -->\n{md}"


class HeaderTest(unittest.TestCase):
    def test_valid(self):
        h, md = rp.parse_proposal(attachment())
        self.assertEqual(md, MD)
        self.assertEqual((h["repo"], h["path"], h["pageId"], h["version"], h["editorName"]),
                         ("owner/name", "docs/prompts/drafter.md", "123", 17, "Ana Kovač"))

    def test_numeric_page_id_crlf_and_bom(self):
        text = "﻿" + attachment(pageId=123).replace(" -->\n", " -->\r\n", 1)
        h, md = rp.parse_proposal(text)
        self.assertEqual((h["pageId"], md), ("123", MD))

    def test_empty_markdown_is_allowed(self):
        h, md = rp.parse_proposal(attachment(md=""))
        self.assertEqual(md, "")

    def test_editor_name_cannot_inject_lines(self):
        h, _ = rp.parse_proposal(attachment(editorName="Eve\nConfluence-page: evil"))
        self.assertNotIn("\n", h["editorName"])
        h, _ = rp.parse_proposal(attachment(editorName=None, editor=None))
        self.assertEqual(h["editorName"], "someone")

    def assertRejected(self, text, reason):
        with self.assertRaises(rp.ProposalError) as cm:
            rp.parse_proposal(text)
        self.assertIn(reason, str(cm.exception))

    def test_missing_fields(self):
        for key in ("repo", "path", "baseSha", "pageId", "version", "proposalHash"):
            self.assertRejected(attachment(**{key: None}), f"no {key}")

    def test_hash_check(self):
        self.assertRejected(attachment(proposalHash="0" * 64), "does not match")
        self.assertRejected(attachment(proposalHash="xyz"), "sha256")
        # one changed byte after the header
        self.assertRejected(attachment()[:-2] + "!\n", "does not match")

    def test_strict_first_line(self):
        good = attachment()
        self.assertRejected(good.replace("<!-- repopages-pending ", "<!--repopages-pending ", 1), "first line")
        self.assertRejected(good.replace(" -->\n", "-->\n", 1), "first line")
        self.assertRejected("\n" + good, "first line")
        self.assertRejected(good.split("\n", 1)[0], "no newline")
        self.assertRejected("<!-- repopages-pending [1] -->\n", "not a JSON object")
        self.assertRejected("<!-- repopages-pending {x} -->\n", "not JSON")

    def test_unsafe_values(self):
        for path in ("/etc/x.md", "../x.md", "docs/../../x.md", "docs\\x.md", "docs/x.txt", ".git/hooks/x.md", "docs//x.md"):
            self.assertRejected(attachment(path=path), "relative Markdown path")
        self.assertRejected(attachment(baseSha="abc"), "baseSha")
        self.assertRejected(attachment(version=0), "positive integer")
        self.assertRejected(attachment(version=True), "positive integer")
        self.assertRejected(attachment(pageId="12a"), "pageId")
        self.assertRejected(attachment(repo="name-only"), "owner/name")

    def test_repo_filter(self):
        self.assertTrue(rp.same_repo("Owner/Name", "owner/name"))
        self.assertTrue(rp.same_repo("group/sub/project", "group/sub/project/"))
        self.assertFalse(rp.same_repo("owner/name", "owner/name2"))


class NamingTest(unittest.TestCase):
    def setUp(self):
        self.h, _ = rp.parse_proposal(attachment())

    def test_branch(self):
        self.assertEqual(rp.edit_branch("repopages/edit", "123", 17), "repopages/edit-123-v17")
        self.assertEqual(rp.edit_branch("bots/cf-", 9, 2), "bots/cf-9-v2")

    def test_commit_message(self):
        self.assertEqual(rp.commit_message(self.h, SITE), (
            "Confluence edit: docs/prompts/drafter.md\n\n"
            "Proposed in Confluence by Ana Kovač on 2026-10-09 (page 123, version 17).\n\n"
            "Edited-in-Confluence-by: Ana Kovač\n"
            "Confluence-page: https://acme.atlassian.net/wiki/pages/viewpage.action?pageId=123\n"))
        msg = rp.commit_message(self.h, SITE, "Needs attention: conflicts.")
        self.assertTrue(msg.endswith("(page 123, version 17).\n\nNeeds attention: conflicts.\n\nEdited-in-Confluence-by: Ana Kovač\n"
                                     "Confluence-page: https://acme.atlassian.net/wiki/pages/viewpage.action?pageId=123\n"), msg)

    def test_title_body_date(self):
        self.assertEqual(rp.edit_title(self.h), "Confluence edit: docs/prompts/drafter.md")
        self.assertEqual(rp.edit_date(None), "an unknown date")
        body = rp.mr_body(self.h, SITE, "main", "2 conflict(s)")
        self.assertIn("pageId=123", body)
        self.assertIn("Needs attention", body)

    def test_author(self):
        self.assertEqual(rp.parse_author(rp.DEFAULT_GIT_AUTHOR), ("RepoPages", "noreply@repopages.app"))
        with self.assertRaises(ValueError):
            rp.parse_author("no email")


class AckTest(unittest.TestCase):
    ENV = {"repo": "owner/name", "ref": "refs/heads/main", "sha": "0123456789abcdef0123456789abcdef01234567",
           "shortSha": "0123456", "commitUrl": "https://github.com/owner/name/commit/0123456789abcdef0123456789abcdef01234567",
           "pushedAt": "2026-10-09T10:00:00+00:00"}

    def test_entry_and_payload(self):
        h, _ = rp.parse_proposal(attachment())
        e = rp.pending_entry(h, "https://github.com/owner/name/pull/42")
        self.assertEqual(e, {"path": "docs/prompts/drafter.md", "pageVersion": 17,
                             "mrUrl": "https://github.com/owner/name/pull/42", "mrState": "open"})
        [p] = rp.ack_payloads(self.ENV, [e])
        self.assertEqual(list(p), ["repo", "ref", "sha", "shortSha", "commitUrl", "pushedAt", "files", "pending"])
        self.assertEqual((p["files"], p["pending"]), ([], [e]))
        self.assertEqual(rp.ack_payloads(self.ENV, []), [])

    def test_at_most_100_per_call(self):
        entries = [{"path": f"d/{i}.md", "pageVersion": 1, "mrUrl": "u"} for i in range(201)]
        self.assertEqual([len(p["pending"]) for p in rp.ack_payloads(self.ENV, entries)], [100, 100, 1])

    def test_shared_vector(self):
        v = json.loads((HERE / "test-vectors" / "writeback-ack-v1.json").read_text("utf-8"))
        self.assertEqual(v["secret"], "0123456789abcdef" * 4)
        self.assertEqual(rp.sign(v["secret"], v["body"].encode("utf-8")), v["signature"])
        self.assertEqual(v["signature"], rp.ACK_VECTOR_SIGNATURE)
        body = json.loads(v["body"])
        self.assertEqual(body["files"], [])
        self.assertEqual([e["mrState"] for e in body["pending"]], ["open", "closed"])
        self.assertEqual(rp.encode(rp.ack_payloads({k: body[k] for k in self.ENV}, body["pending"])[0]), v["body"].encode("utf-8"))


class CqlAndLinksTest(unittest.TestCase):
    def test_cql(self):
        self.assertEqual(rp.pending_cql(), 'label = "repopages-pending" and type = page')
        self.assertEqual(rp.pending_cql("DOCS"), 'label = "repopages-pending" and type = page and space = "DOCS"')
        self.assertTrue(rp.SPACE_KEY_RE.fullmatch("~5570589abc"))
        self.assertFalse(rp.SPACE_KEY_RE.fullmatch('X" or space = "Y'))

    def test_normalize_site(self):
        for s in ("acme.atlassian.net", "https://acme.atlassian.net/", "https://acme.atlassian.net/wiki", " https://acme.atlassian.net/wiki/ "):
            self.assertEqual(rp.normalize_site(s), SITE)
        self.assertEqual(rp.normalize_site(""), "")

    def test_resolve_link(self):
        scoped = rp.SCOPED_API + CLOUD
        cases = [
            (SITE, "/rest/api/content/1/child/attachment/att9/download", SITE + "/wiki/rest/api/content/1/child/attachment/att9/download"),
            (SITE, "/download/attachments/1/repopages-pending.md?version=2&api=v2", SITE + "/wiki/download/attachments/1/repopages-pending.md?version=2&api=v2"),
            (SITE, "/wiki/api/v2/pages/1/attachments?cursor=abc", SITE + "/wiki/api/v2/pages/1/attachments?cursor=abc"),
            (SITE, "rest/api/search?cursor=x", SITE + "/wiki/rest/api/search?cursor=x"),
            (scoped, "/rest/api/search?next=true&cursor=x", scoped + "/wiki/rest/api/search?next=true&cursor=x"),
            (scoped, "/wiki/api/v2/pages/1/attachments", scoped + "/wiki/api/v2/pages/1/attachments"),
            (scoped, SITE + "/wiki/rest/api/content/1/child/attachment/att9/download", scoped + "/wiki/rest/api/content/1/child/attachment/att9/download"),
            (SITE, SITE + "/wiki/x?y=1", SITE + "/wiki/x?y=1"),
            (SITE, "https://api.atlassian.com/ex/confluence/" + CLOUD + "/wiki/rest/api/x", SITE + "/wiki/rest/api/x"),
            (SITE, "https://api.media.atlassian.com/file/abc/binary?token=t", "https://api.media.atlassian.com/file/abc/binary?token=t"),
        ]
        for root, link, want in cases:
            self.assertEqual(rp.resolve_link(root, link), want, (root, link))

    def test_tenant_info(self):
        self.assertEqual(rp.parse_tenant_info(200, json.dumps({"cloudId": CLOUD}).encode()), CLOUD)
        for status, body in ((404, b"{}"), (200, b"<html>"), (200, b"{}"), (200, b'{"cloudId": "../x"}'), (200, b"[]")):
            with self.assertRaises(ValueError):
                rp.parse_tenant_info(status, body)


class FakeConfluence:
    """Answers like Confluence Cloud for pages {id: (title, attachment text or None)}."""

    def __init__(self, pages, accept_site=True, accept_scoped=True, page_size=50):
        self.pages, self.accept_site, self.accept_scoped, self.page_size = pages, accept_site, accept_scoped, page_size
        self.calls = []

    def __call__(self, url, headers, method="GET", data=None):
        self.calls.append((url, dict(headers)))
        u = urllib.parse.urlsplit(url)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/_edge/tenant_info":
            assert "Authorization" not in headers
            return 200, json.dumps({"cloudId": CLOUD}).encode()
        if u.netloc == "media.example":
            assert "Authorization" not in headers, "credentials sent to another host"
            return 200, self.pages[q["page"][0]][1].encode("utf-8")
        scoped = url.startswith(rp.SCOPED_API + CLOUD + "/")
        if not (self.accept_scoped if scoped else self.accept_site) or "Authorization" not in headers:
            return 401, b'{"message":"Unauthorized"}'
        wiki = u.path.split("/wiki", 1)[1]
        if wiki == "/rest/api/search":
            assert q["cql"] == [rp.pending_cql()], q
            ids = sorted(self.pages)
            start = int(q.get("start", ["0"])[0])
            chunk = ids[start:start + self.page_size]
            links = {"base": SITE + "/wiki", "context": "/wiki"}
            if start + self.page_size < len(ids):
                links["next"] = "/rest/api/search?" + urllib.parse.urlencode({"cql": q["cql"][0], "start": start + self.page_size})
            return 200, json.dumps({"results": [{"content": {"id": i, "type": "page", "title": self.pages[i][0]}, "title": self.pages[i][0]}
                                                for i in chunk], "_links": links}).encode()
        if wiki.startswith("/api/v2/pages/") and wiki.endswith("/attachments"):
            pid = wiki.split("/")[4]
            assert q["filename"] == [rp.PENDING_ATTACHMENT]
            text = self.pages[pid][1]
            results = []
            if text is not None:
                results = [{"id": "att9", "title": rp.PENDING_ATTACHMENT, "status": "current", "pageId": pid,
                            "downloadLink": f"/rest/api/content/{pid}/child/attachment/att9/download",
                            "_links": {"download": f"/download/attachments/{pid}/{rp.PENDING_ATTACHMENT}"}}]
            return 200, json.dumps({"results": results, "_links": {"base": SITE + "/wiki"}}).encode()
        if wiki.endswith("/child/attachment/att9/download"):
            return 302, f"https://media.example/file?page={wiki.split('/')[4]}"
        return 404, b"{}"


def follow(fake):
    """Wrap a FakeConfluence so a 302 is followed like urllib with _SameHostAuth does."""
    def get(url, headers, method="GET", data=None):
        status, body = fake(url, headers, method, data)
        if status == 302:
            h = dict(headers)
            if urllib.parse.urlsplit(body).netloc != urllib.parse.urlsplit(url).netloc:
                h.pop("Authorization", None)
            return fake(body, h)
        return status, body
    return get


class ConfluenceClientTest(unittest.TestCase):
    PAGES = {"123": ("Drafter", attachment()), "124": ("No proposal", None)}

    def client(self, fake, **kw):
        logs = []
        return rp.Confluence("acme.atlassian.net", "a@b.c", "tok-123456", get=follow(fake), log=logs.append, **kw), logs

    def test_classic_token(self):
        fake = FakeConfluence(self.PAGES)
        c, logs = self.client(fake)
        self.assertEqual(c.search_pending(), [("123", "Drafter"), ("124", "No proposal")])
        self.assertEqual(c.pending_attachment("123").decode("utf-8"), attachment())
        self.assertIsNone(c.pending_attachment("124"))
        self.assertFalse(c.scoped)
        self.assertTrue(all(u.startswith(SITE) or "media.example" in u for u, _ in fake.calls))

    def test_pagination(self):
        pages = {str(i): (f"p{i}", None) for i in range(100, 107)}
        c, _ = self.client(FakeConfluence(pages, page_size=3))
        self.assertEqual([p for p, _ in c.search_pending()], sorted(pages))

    def test_falls_back_to_scoped_form_on_401(self):
        fake = FakeConfluence(self.PAGES, accept_site=False)
        c, logs = self.client(fake)
        self.assertEqual(len(c.search_pending()), 2)
        self.assertTrue(c.scoped)
        self.assertEqual(c.root, rp.SCOPED_API + CLOUD)
        self.assertEqual(c.pending_attachment("123").decode("utf-8"), attachment())
        self.assertIn("scoped-token form", logs[0])
        self.assertEqual([u for u, _ in fake.calls][:2], [SITE + "/wiki/rest/api/search?" + urllib.parse.urlencode(
            {"cql": rp.pending_cql(), "limit": 50}), SITE + "/_edge/tenant_info"])

    def test_scoped_flag_skips_the_site(self):
        fake = FakeConfluence(self.PAGES, accept_site=False)
        c, logs = self.client(fake, scoped=True)
        c.search_pending()
        self.assertFalse(any(u.startswith(SITE + "/wiki") for u, _ in fake.calls))
        self.assertEqual(logs, [])

    def test_bad_token_is_401_after_one_fallback(self):
        c, _ = self.client(FakeConfluence(self.PAGES, accept_site=False, accept_scoped=False))
        with self.assertRaises(rp.ConfluenceError) as cm:
            c.search_pending()
        self.assertEqual(cm.exception.status, 401)
        self.assertIn("search:confluence", str(cm.exception))

    def test_later_403_does_not_switch(self):
        fake = FakeConfluence(self.PAGES)
        c, _ = self.client(fake)
        c.search_pending()
        fake.accept_site = False
        with self.assertRaises(rp.ConfluenceError):
            c.pending_attachment("123")
        self.assertFalse(c.scoped)

    def test_real_redirect_handler_drops_authorization(self):
        import urllib.request
        req = urllib.request.Request(SITE + "/wiki/x", headers={"Authorization": "Basic abc"})
        h = rp._SameHostAuth()
        other = h.redirect_request(req, None, 302, "Found", {}, "https://api.media.atlassian.com/file")
        same = h.redirect_request(req, None, 302, "Found", {}, SITE + "/wiki/y")
        self.assertNotIn("Authorization", other.headers)
        self.assertEqual(same.headers.get("Authorization"), "Basic abc")


class MergeTest(unittest.TestCase):
    BASE = "# T\n\nintro\n\nmiddle\n\nend\n"

    def test_clean_cases(self):
        self.assertEqual(rp.merge_proposal(self.BASE, self.BASE, "new\n"), ("new\n", "", "clean"))
        self.assertEqual(rp.merge_proposal(None, None, "new\n")[0:2], ("new\n", ""))
        self.assertEqual(rp.merge_proposal(self.BASE + "x\n", None, "new\n")[0:2], ("new\n", ""))
        self.assertIn("deleted", rp.merge_proposal(None, self.BASE, "new\n")[1])

    def test_three_way(self):
        ours = self.BASE.replace("end", "end (from Git)")
        theirs = self.BASE.replace("intro", "intro (from Confluence)")
        merged, attention, note = rp.merge_proposal(ours, self.BASE, theirs)
        self.assertEqual(merged, "# T\n\nintro (from Confluence)\n\nmiddle\n\nend (from Git)\n")
        self.assertEqual(attention, "")

    def test_conflict_keeps_markers(self):
        ours = self.BASE.replace("middle", "middle A")
        theirs = self.BASE.replace("middle", "middle B")
        merged, attention, note = rp.merge_proposal(ours, self.BASE, theirs, labels=("main (Git)", "base", "Confluence"))
        self.assertIn("<<<<<<< main (Git)\nmiddle A\n=======\nmiddle B\n>>>>>>> Confluence\n", merged)
        self.assertIn("1 conflict", attention)

    def test_crlf_repository(self):
        crlf = self.BASE.replace("\n", "\r\n")
        merged, _, _ = rp.merge_proposal(crlf, crlf, "# T\n\nnew\n")
        self.assertEqual(merged, "# T\r\n\r\nnew\r\n")


def git(cwd, *args):
    r = subprocess.run(["git", "-c", "user.name=Dev", "-c", "user.email=dev@example.com", "-c", "init.defaultBranch=main",
                        "-c", "commit.gpgsign=false", *args], cwd=cwd, capture_output=True, text=True)
    if r.returncode:
        raise AssertionError(f"git {args}: {r.stderr}")
    return r.stdout.strip()


class FakeHost:
    kind = "pull request"
    created, existing, fail = [], {}, False

    def __init__(self, repo, env=None, **_):
        self.repo = repo

    def find(self, branch):
        return FakeHost.existing.get(branch)

    def create(self, base, branch, title, body, labels=()):
        if FakeHost.fail:
            raise rp.HostError("gh pr create: HTTP 403 GitHub Actions is not permitted to create pull requests")
        FakeHost.created.append((base, branch, title, list(labels)))
        return f"https://github.com/owner/name/pull/{len(FakeHost.created)}"


class FlowTest(unittest.TestCase):
    """cmd_pull_edits end to end against a local bare 'origin', a fake Confluence, gh and sync trigger."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="repopages-test-")
        t = Path(self.tmp.name)
        self.origin, self.work = t / "origin.git", t / "work"
        git(t, "init", "--bare", "-q", str(self.origin))
        git(t, "clone", "-q", str(self.origin), str(self.work))
        (self.work / "docs" / "prompts").mkdir(parents=True)
        self.v1 = "# Drafter\n\nintro\n\nmiddle\n\nend\n"
        (self.work / "docs/prompts/drafter.md").write_text(self.v1)
        git(self.work, "add", "-A")
        git(self.work, "commit", "-qm", "v1")
        self.base_sha = git(self.work, "rev-parse", "HEAD")
        git(self.work, "push", "-q", "origin", "HEAD:main")
        git(self.work, "remote", "set-head", "origin", "main")
        self.status_before = git(self.work, "status", "--porcelain")
        self.proposal = self.v1.replace("intro", "intro, edited in Confluence")
        self.pages = {"123": ("Drafter", attachment(md=self.proposal, baseSha=self.base_sha)),
                      "124": ("Unsupported", None),
                      "125": ("Broken", attachment(md="x\n", pageId="125", proposalHash="0" * 64)),
                      "126": ("Other repo", attachment(md="y\n", pageId="126", repo="owner/other"))}
        self.posts = []
        FakeHost.created, FakeHost.existing, FakeHost.fail = [], {}, False
        self.ack_status = 200

    def tearDown(self):
        self.tmp.cleanup()

    def run_cmd(self, *args, env=None):
        environ = {k: v for k, v in os.environ.items() if not k.startswith(("GITHUB_", "GITLAB", "CI_", "REPOPAGES_", "CONFLUENCE_", "GH_"))}
        environ.update({"CONFLUENCE_SITE": "acme.atlassian.net", "CONFLUENCE_EMAIL": "bot@acme.com", "CONFLUENCE_TOKEN": "secret-token-xyz",
                        "REPOPAGES_URL": "https://example.invalid/sync", "REPOPAGES_SECRET": "ab" * 32})
        environ.update(env or {})

        def fake_post(url, body, signature):
            self.posts.append((url, json.loads(body), signature))
            return self.ack_status, '{"status":"ok"}'

        a = rp.build_parser().parse_args(["pull-edits", "--repo", "owner/name", "--repo-dir", str(self.work), *args])
        out = io.StringIO()
        with mock.patch.dict(os.environ, environ, clear=True), \
                mock.patch.object(rp, "http_request", follow(FakeConfluence(self.pages))), \
                mock.patch.object(rp, "post", fake_post), mock.patch.object(rp, "GitHubHost", FakeHost), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                rc = rp.cmd_pull_edits(a)
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue()

    def remote_branches(self):
        return git(self.origin, "for-each-ref", "--format=%(refname:short)", "refs/heads/").split()

    def test_mr_none_pushes_branch_only(self):
        rc, out = self.run_cmd("--mr", "none")
        self.assertEqual(rc, 0, out)
        self.assertEqual(sorted(self.remote_branches()), ["main", "repopages/edit-123-v17"])
        self.assertEqual(git(self.origin, "show", "repopages/edit-123-v17:docs/prompts/drafter.md") + "\n", self.proposal)
        log = git(self.origin, "log", "-1", "--format=%an <%ae>|%cn|%P|%B", "repopages/edit-123-v17")
        self.assertTrue(log.startswith(f"RepoPages <noreply@repopages.app>|RepoPages|{self.base_sha}|Confluence edit: docs/prompts/drafter.md"), log)
        self.assertIn("Edited-in-Confluence-by: Ana Kovač", log)
        self.assertIn("Confluence-page: https://acme.atlassian.net/wiki/pages/viewpage.action?pageId=123", log)
        self.assertIn("no repopages-pending.md", out)
        self.assertIn("WARN page 125", out)
        self.assertIn("proposal for owner/other", out)
        self.assertNotIn("secret-token-xyz", out)
        self.assertEqual(self.posts, [])
        # the caller's checkout is untouched and no worktree is left behind
        self.assertEqual(git(self.work, "status", "--porcelain"), self.status_before)
        self.assertEqual(len(git(self.work, "worktree", "list").splitlines()), 1)
        self.assertEqual(git(self.work, "branch", "--format=%(refname:short)"), "main")
        # a second run finds the branch and does not push again
        rc, out = self.run_cmd("--mr", "none")
        self.assertEqual(rc, 0, out)
        self.assertIn("repopages/edit-123-v17 exists (--mr none", out)

    def test_github_opens_pr_and_acks(self):
        rc, out = self.run_cmd("--mr", "github")
        self.assertEqual(rc, 0, out)
        self.assertEqual(FakeHost.created, [("main", "repopages/edit-123-v17", "Confluence edit: docs/prompts/drafter.md", [])])
        [(url, body, sig)] = self.posts
        self.assertEqual(body["files"], [])
        self.assertEqual(body["pending"], [{"path": "docs/prompts/drafter.md", "pageVersion": 17,
                                            "mrUrl": "https://github.com/owner/name/pull/1", "mrState": "open"}])
        self.assertEqual((body["repo"], body["ref"], body["sha"]), ("owner/name", "refs/heads/main", self.base_sha))
        self.assertEqual(sig, rp.sign("ab" * 32, rp.encode(body)))
        # the next run re-acknowledges the existing PR with its current state
        FakeHost.existing = {"repopages/edit-123-v17": ("https://github.com/owner/name/pull/1", "merged")}
        rc, out = self.run_cmd("--mr", "github")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.posts[-1][1]["pending"][0]["mrState"], "merged")
        self.assertEqual(len(FakeHost.created), 1)

    def test_conflict_gets_label_and_markers(self):
        (self.work / "docs/prompts/drafter.md").write_text(self.v1.replace("intro", "intro, changed in Git"))
        git(self.work, "commit", "-qam", "v2")
        git(self.work, "push", "-q", "origin", "HEAD:main")
        rc, out = self.run_cmd("--mr", "github")
        self.assertEqual(rc, 0, out)
        self.assertEqual(FakeHost.created[0][3], ["needs-attention"])
        content = git(self.origin, "show", "repopages/edit-123-v17:docs/prompts/drafter.md")
        self.assertIn("<<<<<<< main (Git)", content)
        self.assertIn(">>>>>>> Confluence", content)

    def test_dry_run_writes_nothing(self):
        rc, out = self.run_cmd("--mr", "github", "--dry-run", env={"REPOPAGES_URL": "", "REPOPAGES_SECRET": ""})
        self.assertEqual(rc, 0, out)
        self.assertIn("would push repopages/edit-123-v17", out)
        self.assertEqual(self.remote_branches(), ["main"])
        self.assertEqual((FakeHost.created, self.posts), ([], []))

    def test_exit_codes(self):
        self.ack_status = 401
        rc, out = self.run_cmd("--mr", "github")
        self.assertEqual(rc, 3, out)
        FakeHost.fail = True
        self.pages["123"] = ("Drafter", attachment(md=self.proposal, baseSha=self.base_sha, version=18))
        rc, out = self.run_cmd("--mr", "github")
        self.assertEqual(rc, 4, out)
        self.assertIn("could not open", out)
        # the next run finds the pushed branch without a pull request and opens it
        FakeHost.fail = False
        self.ack_status = 200
        rc, out = self.run_cmd("--mr", "github")
        self.assertEqual(rc, 0, out)
        self.assertIn("opened https://github.com/owner/name/pull/2 from the existing repopages/edit-123-v18", out)
        self.assertEqual(self.posts[-1][1]["pending"][0]["pageVersion"], 18)

    def test_no_ack_and_config_errors(self):
        rc, out = self.run_cmd("--mr", "github", "--no-ack", env={"REPOPAGES_URL": "", "REPOPAGES_SECRET": ""})
        self.assertEqual((rc, self.posts), (0, []), out)
        rc, out = self.run_cmd("--mr", "github", env={"REPOPAGES_URL": ""})
        self.assertEqual(rc, 2, out)
        rc, out = self.run_cmd("--mr", "none", env={"CONFLUENCE_TOKEN": ""})
        self.assertEqual(rc, 2, out)
        rc, out = self.run_cmd("--mr", "none", "--space", 'X" or space = "Y')
        self.assertEqual(rc, 2, out)


class HostTest(unittest.TestCase):
    def test_github_find_and_create(self):
        calls = []

        def run(args):
            calls.append(args)
            if args[:2] == ["pr", "list"]:
                return 0, '[{"url":"https://github.com/o/r/pull/7","state":"CLOSED"}]', ""
            if args[:2] == ["pr", "create"]:
                return 0, "https://github.com/o/r/pull/8\n", ""
            if args[:2] == ["pr", "edit"] and not any(c[:2] == ["label", "create"] for c in calls):
                return 1, "", "could not add label: 'needs-attention' not found"
            return 0, "", ""

        h = rp.GitHubHost("o/r", run=run, env={"GITHUB_TOKEN": "ghs_x"})
        self.assertEqual(h.env["GH_TOKEN"], "ghs_x")
        self.assertEqual(h.find("b"), ("https://github.com/o/r/pull/7", "closed"))
        self.assertEqual(h.create("main", "b", "t", "body", ["needs-attention"]), "https://github.com/o/r/pull/8")
        self.assertEqual([c[:2] for c in calls], [["pr", "list"], ["pr", "create"], ["pr", "edit"], ["label", "create"], ["pr", "edit"]])
        self.assertNotIn("--force", sum(calls, []))

    def test_gitlab(self):
        seen = []

        def request(url, headers, method="GET", data=None):
            seen.append((url, headers, method, json.loads(data) if data else None))
            if method == "GET":
                return 200, b'[{"web_url":"https://gl/x/-/merge_requests/3","state":"opened"}]'
            return 201, b'{"web_url":"https://gl/x/-/merge_requests/4"}'

        h = rp.GitLabHost("group/proj", env={"CI_API_V4_URL": "https://gl/api/v4", "CI_PROJECT_ID": "42", "CI_JOB_TOKEN": "jt"}, request=request)
        self.assertEqual(h.find("repopages/edit-1-v2"), ("https://gl/x/-/merge_requests/3", "open"))
        self.assertEqual(h.create("main", "b", "t", "d", ["needs-attention"]), "https://gl/x/-/merge_requests/4")
        self.assertEqual(seen[0][0], "https://gl/api/v4/projects/42/merge_requests?source_branch=repopages%2Fedit-1-v2&state=all&per_page=1")
        self.assertEqual(seen[1][1]["JOB-TOKEN"], "jt")
        self.assertEqual(seen[1][3]["labels"], "needs-attention")
        h = rp.GitLabHost("group/proj", env={"GITLAB_TOKEN": "glpat"}, request=request)
        self.assertEqual((h.api, h.project, h.headers), ("https://gitlab.com/api/v4", "group%2Fproj", {"PRIVATE-TOKEN": "glpat"}))
        self.assertEqual(rp.gitlab_push_url({"GITLAB_TOKEN": "glpat", "CI_SERVER_URL": "https://gl.example", "CI_PROJECT_PATH": "g/p"}),
                         "https://oauth2:glpat@gl.example/g/p.git")

    def test_default_mr(self):
        self.assertEqual(rp.default_mr({"GITHUB_ACTIONS": "true"}), "github")
        self.assertEqual(rp.default_mr({"GITLAB_CI": "true"}), "gitlab")
        self.assertEqual(rp.default_mr({}), "none")


if __name__ == "__main__":
    unittest.main()

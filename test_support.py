import json
import os
import tempfile
import unittest

import community


class SupportTicketApiTests(unittest.TestCase):
    ip_counter = 2

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.originals = (community.DATA_DIR, community.DB_PATH, community.SECRET_KEY_FILE)
        community.DATA_DIR = self.temporary.name
        community.DB_PATH = os.path.join(self.temporary.name, "support-test.db")
        community.SECRET_KEY_FILE = os.path.join(self.temporary.name, "secret.key")
        community.init()
        self.ip = f"127.0.0.{self.ip_counter}"
        type(self).ip_counter += 1
        self.admin_cookie = self.register("team", "team@example.test")
        self.member_cookie = self.register("member", "member@example.test")
        self.other_cookie = self.register("other", "other@example.test")

    def tearDown(self):
        community.DATA_DIR, community.DB_PATH, community.SECRET_KEY_FILE = self.originals
        self.temporary.cleanup()

    def request(self, method, path, payload=None, cookie=""):
        headers = {"Host": "localhost:8766", "Origin": "http://localhost:8766", "X-Rhetora": "1"}
        if cookie:
            headers["Cookie"] = cookie
        status, data, response_headers = community.handle(
            method, path, "", headers,
            json.dumps(payload).encode() if payload is not None else b"", self.ip,
        )
        next_cookie = next((value.split(";")[0] for key, value in response_headers if key.lower() == "set-cookie"), cookie)
        return status, data, next_cookie

    def register(self, username, email):
        status, _, cookie = self.request("POST", "/api/auth/register", {
            "username": username, "email": email, "password": "Strong-pass-123!",
        })
        self.assertEqual(status, 201)
        return cookie

    def create_ticket(self):
        status, data, _ = self.request("POST", "/api/me/support", {
            "subject": "Question de test", "category": "question", "body": "Bonjour l’équipe.",
        }, self.member_cookie)
        self.assertEqual(status, 200, data)
        return data["ticket_id"]

    def test_member_and_staff_can_exchange_replies(self):
        ticket_id = self.create_ticket()

        status, inbox, _ = self.request("GET", "/api/admin/support", cookie=self.admin_cookie)
        self.assertEqual(status, 200)
        self.assertEqual(inbox["tickets"][0]["id"], ticket_id)
        self.assertEqual(inbox["tickets"][0]["unread"], 1)
        with community.tx() as conn:
            team_notice = conn.execute("SELECT COUNT(*) FROM notifications WHERE user_id = (SELECT id FROM users WHERE username = 'team') AND kind = 'support'").fetchone()[0]
        self.assertEqual(team_notice, 1)

        status, _, _ = self.request("POST", f"/api/admin/support/{ticket_id}/messages",
                                    {"body": "Réponse de l’équipe."}, self.admin_cookie)
        self.assertEqual(status, 200)
        status, thread, _ = self.request("GET", f"/api/me/support/{ticket_id}", cookie=self.member_cookie)
        self.assertEqual(status, 200)
        self.assertEqual(thread["ticket"]["status"], "waiting")
        self.assertEqual([message["body"] for message in thread["messages"]],
                         ["Bonjour l’équipe.", "Réponse de l’équipe."])
        with community.tx() as conn:
            member_notice = conn.execute("SELECT COUNT(*) FROM notifications WHERE user_id = (SELECT id FROM users WHERE username = 'member') AND kind = 'support'").fetchone()[0]
        self.assertEqual(member_notice, 1)

        status, _, _ = self.request("POST", f"/api/me/support/{ticket_id}/messages",
                                    {"body": "Merci, je précise ma question."}, self.member_cookie)
        self.assertEqual(status, 200)
        status, thread, _ = self.request("GET", f"/api/me/support/{ticket_id}", cookie=self.member_cookie)
        self.assertEqual(status, 200)
        self.assertEqual(thread["ticket"]["status"], "open")
        self.assertEqual(len(thread["messages"]), 3)

    def test_ticket_is_private_and_admin_can_resolve_it(self):
        ticket_id = self.create_ticket()
        status, data, _ = self.request("GET", f"/api/me/support/{ticket_id}", cookie=self.other_cookie)
        self.assertEqual(status, 404)

        status, data, _ = self.request("PATCH", f"/api/admin/support/{ticket_id}",
                                       {"status": "closed"}, self.admin_cookie)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["status"], "closed")

    def test_invalid_category_is_rejected(self):
        status, data, _ = self.request("POST", "/api/me/support", {
            "subject": "Question", "category": "admin", "body": "Test",
        }, self.member_cookie)
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
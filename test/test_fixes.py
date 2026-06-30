"""
Programmatic unit tests to verify the backend bug fixes (W103 - W150).
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest
from datetime import datetime, date, time, timezone
from enum import Enum
from bson import ObjectId
from pydantic import BaseModel, ValidationError

# 1. Test Encryption key validation
from utils.encryption import _get_encryption_key
from config import get_settings

# 2. Test Redis JSON serializer
from redis_client import _json_default

# 3. Test Models
from models.meeting import MeetingBase, MeetingCreate, MeetingUpdate, MeetingType
from models.leave import LeaveBase, LeaveType
from models.sprint import SprintBase, SprintStatus
from models.bug import EnvironmentInfo, EnvironmentType
from models.workflow import WorkflowBase, WorkflowState, WorkflowTransition
from models.custom_field import CustomFieldDefinition, CustomFieldCreate, CustomFieldValue, CustomFieldType, _clean_options
from models.ai_chat import ChatConversation, ChatMessage, MessageRole, ConversationStatus
from models.document import DocumentInDB, DocumentFormat, DocumentStatus
from models.off_project_task import OffProjectTaskBase, OffProjectTaskStatus, OffProjectTaskPriority
from models.user import UserBase, UserUpdate, UserRole


class TestBugFixes(unittest.TestCase):
    def test_w111_encryption_key_validation(self):
        settings = get_settings()
        original_key = settings.encryption_key
        try:
            # Test empty key
            settings.encryption_key = ""
            with self.assertRaises(ValueError) as ctx:
                _get_encryption_key()
            self.assertIn("ENCRYPTION_KEY not set", str(ctx.exception))

            # Test short key
            settings.encryption_key = "abc123"
            with self.assertRaises(ValueError) as ctx:
                _get_encryption_key()
            self.assertIn("64-character hex string", str(ctx.exception))

            # Test invalid hex character key
            settings.encryption_key = "z" * 64
            with self.assertRaises(ValueError) as ctx:
                _get_encryption_key()
            self.assertIn("valid hexadecimal string", str(ctx.exception))
        finally:
            settings.encryption_key = original_key

    def test_w121_redis_json_serializer(self):
        # Test datetime serialization
        dt = datetime(2026, 6, 24, 10, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(_json_default(dt), "2026-06-24T10:00:00+00:00")

        # Test date serialization
        d = date(2026, 6, 24)
        self.assertEqual(_json_default(d), "2026-06-24")

        # Test Enum serialization
        class MockEnum(Enum):
            VAL = "test_value"
        self.assertEqual(_json_default(MockEnum.VAL), "test_value")

        # Test ObjectId serialization
        oid = ObjectId("60d5ec49f1b2c43d78901234")
        self.assertEqual(_json_default(oid), "60d5ec49f1b2c43d78901234")

        # Test BaseModel serialization
        class MockModel(BaseModel):
            field: str
        model = MockModel(field="hello")
        self.assertEqual(_json_default(model), {"field": "hello"})

        # Test unsupported type
        with self.assertRaises(TypeError):
            _json_default(set())

    def test_w131_comment_entity_type_literal(self):
        from models.comment import CommentBase
        # Valid entity_type
        c1 = CommentBase(content="A comment", entity_type="task", entity_id="60d5ec49f1b2c43d78901234")
        self.assertEqual(c1.entity_type, "task")
        c2 = CommentBase(content="A comment", entity_type="bug", entity_id="60d5ec49f1b2c43d78901234")
        self.assertEqual(c2.entity_type, "bug")

        # Invalid entity_type
        with self.assertRaises(ValidationError):
            CommentBase(content="A comment", entity_type="epic", entity_id="60d5ec49f1b2c43d78901234")

    def test_w132_issue_link_self_link_prevention(self):
        from models.issue_link import IssueLinkCreate, LinkType
        # Valid link
        link = IssueLinkCreate(source_id="60d5ec49f1b2c43d78901234", target_id="60d5ec49f1b2c43d78901235", link_type=LinkType.BLOCKS)
        self.assertEqual(link.source_id, "60d5ec49f1b2c43d78901234")

        # Self-link should fail
        with self.assertRaises(ValidationError) as ctx:
            IssueLinkCreate(source_id="60d5ec49f1b2c43d78901234", target_id="60d5ec49f1b2c43d78901234", link_type=LinkType.BLOCKS)
        self.assertIn("self-linking is not allowed", str(ctx.exception))

    def test_w133_task_model_self_dependency_prevention(self):
        from models.task import TaskCreate, TaskUpdate, TaskRole, TaskPriority, TaskStatus, TaskComplexity
        # Valid task
        t = TaskCreate(
            title="Task",
            project_id="60d5ec49f1b2c43d78901234",
            parent_id="60d5ec49f1b2c43d78901235",
            dependencies=["60d5ec49f1b2c43d78901236"],
            role=TaskRole.BACKEND,
            priority=TaskPriority.MEDIUM,
            status=TaskStatus.TODO,
            complexity=TaskComplexity.MEDIUM,
        )
        self.assertEqual(t.parent_id, "60d5ec49f1b2c43d78901235")

        # parent_id in dependencies should fail
        with self.assertRaises(ValidationError) as ctx:
            TaskCreate(
                title="Task",
                project_id="60d5ec49f1b2c43d78901234",
                parent_id="60d5ec49f1b2c43d78901235",
                dependencies=["60d5ec49f1b2c43d78901235"],
                role=TaskRole.BACKEND,
                priority=TaskPriority.MEDIUM,
                status=TaskStatus.TODO,
                complexity=TaskComplexity.MEDIUM,
            )
        self.assertIn("parent_id cannot be in dependencies", str(ctx.exception))

    def test_w134_w135_w136_meeting_validators(self):
        # Valid meeting
        m = MeetingBase(
            title="Standup",
            date=date(2026, 6, 24),
            time=time(9, 30),
            duration=30,
            attendees=["60d5ec49f1b2c43d78901234", "60d5ec49f1b2c43d78901235"]
        )
        self.assertEqual(m.duration, 30)
        self.assertEqual(len(m.attendees), 2)

        # Invalid duration (zero or negative)
        with self.assertRaises(ValidationError):
            MeetingBase(title="Standup", date=date(2026, 6, 24), time=time(9, 30), duration=0)

        # Invalid duration (too large)
        with self.assertRaises(ValidationError):
            MeetingBase(title="Standup", date=date(2026, 6, 24), time=time(9, 30), duration=2000)

        # Invalid attendee ID
        with self.assertRaises(ValidationError) as ctx:
            MeetingBase(
                title="Standup",
                date=date(2026, 6, 24),
                time=time(9, 30),
                duration=30,
                attendees=["invalid_id"]
            )
        self.assertIn("must be a valid 24-character hex string", str(ctx.exception))

        # Deduplication of attendees
        m_dedup = MeetingBase(
            title="Standup",
            date=date(2026, 6, 24),
            time=time(9, 30),
            duration=30,
            attendees=["60d5ec49f1b2c43d78901234", "60d5ec49f1b2c43d78901234"]
        )
        self.assertEqual(len(m_dedup.attendees), 1)

    def test_w137_leave_date_ordering(self):
        # Valid leave
        l = LeaveBase(leave_type=LeaveType.ANNUAL, start_date=date(2026, 6, 24), end_date=date(2026, 6, 25), reason="Vacation")
        self.assertEqual(l.reason, "Vacation")

        # Invalid leave (start_date > end_date)
        with self.assertRaises(ValidationError) as ctx:
            LeaveBase(leave_type=LeaveType.ANNUAL, start_date=date(2026, 6, 25), end_date=date(2026, 6, 24), reason="Vacation")
        self.assertIn("start_date must be on or before end_date", str(ctx.exception))

    def test_w138_sprint_date_ordering(self):
        # Valid sprint
        s = SprintBase(name="Sprint 1", start_date=datetime(2026, 6, 24), end_date=datetime(2026, 7, 8))
        self.assertEqual(s.name, "Sprint 1")

        # Invalid sprint
        with self.assertRaises(ValidationError) as ctx:
            SprintBase(name="Sprint 1", start_date=datetime(2026, 7, 8), end_date=datetime(2026, 6, 24))
        self.assertIn("start_date must be on or before end_date", str(ctx.exception))

    def test_w139_bug_environment_info_non_empty(self):
        # Valid environment
        env = EnvironmentInfo(os=EnvironmentType.WINDOWS)
        self.assertEqual(env.os, EnvironmentType.WINDOWS)

        # Empty environment should fail
        with self.assertRaises(ValidationError) as ctx:
            EnvironmentInfo()
        self.assertIn("At least one environment field", str(ctx.exception))

    def test_w140_w141_w142_workflow_validators(self):
        s1 = WorkflowState(id="todo", label="To Do")
        s2 = WorkflowState(id="done", label="Done")
        t1 = WorkflowTransition(from_status="todo", to_status="done")

        # Valid workflow
        w = WorkflowBase(name="Standard", states=[s1, s2], transitions=[t1], default_state="todo")
        self.assertEqual(w.default_state, "todo")

        # Invalid state ID (contains spaces)
        with self.assertRaises(ValidationError) as ctx:
            WorkflowState(id="to do", label="To Do")
        self.assertIn("State ID must be alphanumeric", str(ctx.exception))

        # Non-unique state IDs
        with self.assertRaises(ValidationError) as ctx:
            WorkflowBase(name="Standard", states=[s1, s1], transitions=[], default_state="todo")
        self.assertIn("All state IDs must be unique", str(ctx.exception))

        # Default state not in defined states
        with self.assertRaises(ValidationError) as ctx:
            WorkflowBase(name="Standard", states=[s1], transitions=[], default_state="done")
        self.assertIn("default_state 'done' must match one of the defined state IDs", str(ctx.exception))

        # Transition using undefined state
        with self.assertRaises(ValidationError) as ctx:
            WorkflowBase(name="Standard", states=[s1], transitions=[t1], default_state="todo")
        self.assertIn("Transition to_status 'done' is not a defined state ID", str(ctx.exception))

    def test_w143_custom_field_options_cleaning(self):
        opts = [" Option A ", "Option B", "  ", "Option A", "A" * 105]
        
        # Test options sanitizer helper directly
        with self.assertRaises(ValueError) as ctx:
            _clean_options(opts)
        self.assertIn("Custom field option length exceeds limit", str(ctx.exception))

        # Test valid options cleaning
        valid_opts = [" Option A ", "Option B", "", "Option A"]
        cleaned = _clean_options(valid_opts)
        self.assertEqual(cleaned, ["Option A", "Option B"])

    def test_w145_ai_chat_messages_limit(self):
        # Valid chat
        msg = ChatMessage(role=MessageRole.USER, content="Hello")
        chat = ChatConversation(user_id="60d5ec49f1b2c43d78901234", title="Conversation", messages=[msg] * 10)
        self.assertEqual(len(chat.messages), 10)

        # Excess messages should fail
        with self.assertRaises(ValidationError) as ctx:
            ChatConversation(user_id="60d5ec49f1b2c43d78901234", title="Conversation", messages=[msg] * 501)
        self.assertIn("Conversation history cannot exceed 500 messages", str(ctx.exception))

    def test_w146_w147_document_validation(self):
        # Valid document
        doc = DocumentInDB(
            user_id="123",
            format=DocumentFormat.DOC,
            prompt="Write a report",
            file_name="report.docx",
            file_path="uploads/documents/report.docx",
            size_bytes=5000,
        )
        self.assertEqual(doc.size_bytes, 5000)

        # Path traversal in file_path should fail
        with self.assertRaises(ValidationError) as ctx:
            DocumentInDB(
                user_id="123",
                format=DocumentFormat.DOC,
                prompt="Write a report",
                file_name="report.docx",
                file_path="uploads/documents/../../etc/passwd",
                size_bytes=5000,
            )
        self.assertIn("Directory traversal characters", str(ctx.exception))

        # Negative size should fail
        with self.assertRaises(ValidationError):
            DocumentInDB(
                user_id="123",
                format=DocumentFormat.DOC,
                prompt="Write a report",
                file_name="report.docx",
                file_path="uploads/documents/report.docx",
                size_bytes=-100,
            )

        # Excessive size (> 100 MB) should fail
        with self.assertRaises(ValidationError):
            DocumentInDB(
                user_id="123",
                format=DocumentFormat.DOC,
                prompt="Write a report",
                file_name="report.docx",
                file_path="uploads/documents/report.docx",
                size_bytes=200 * 1024 * 1024,
            )

    def test_w148_off_project_task_hours(self):
        # Valid off project task
        t = OffProjectTaskBase(title="Off Task", estimated_hours=4.5, actual_hours=2.0)
        self.assertEqual(t.estimated_hours, 4.5)

        # Negative estimated hours should fail
        with self.assertRaises(ValidationError):
            OffProjectTaskBase(title="Off Task", estimated_hours=-1.0)

        # Negative actual hours should fail
        with self.assertRaises(ValidationError):
            OffProjectTaskBase(title="Off Task", actual_hours=-0.5)

    def test_w149_w150_user_validators(self):
        # Valid user
        u = UserBase(
            email="dev@by8flow.com",
            name="Developer",
            role=UserRole.DEVELOPER,
            avatar="https://by8flow.com/avatars/dev.png",
        )
        self.assertEqual(u.role, UserRole.DEVELOPER)

        # Invalid role (coerced or checked)
        with self.assertRaises(ValidationError):
            UserBase(email="dev@by8flow.com", name="Developer", role="invalid_role")

        # Invalid avatar URL format
        with self.assertRaises(ValidationError) as ctx:
            UserBase(
                email="dev@by8flow.com",
                name="Developer",
                role=UserRole.DEVELOPER,
                avatar="invalid_avatar_url",
            )
        self.assertIn("Avatar must be a valid URL", str(ctx.exception))

    def test_l93_workflow_transition_role_enum(self):
        # Valid transition with UserRole
        t1 = WorkflowTransition(from_status="todo", to_status="done", require_role=UserRole.ADMIN)
        self.assertEqual(t1.require_role, UserRole.ADMIN)

        # Invalid transition role should fail validation
        with self.assertRaises(ValidationError):
            WorkflowTransition(from_status="todo", to_status="done", require_role="invalid_role")

    def test_l105_salted_sha256_otp(self):
        from utils.security import hash_otp, verify_otp
        settings = get_settings()
        original_secret = settings.jwt_secret
        if not settings.jwt_secret:
            settings.jwt_secret = "test-secret-key-12345"
        try:
            plain_otp = "123456"
            hashed = hash_otp(plain_otp)
            self.assertTrue(verify_otp(plain_otp, hashed))
            self.assertFalse(verify_otp("111111", hashed))
            # Ensure it is not bcrypt (bcrypt hashes start with $2)
            self.assertFalse(hashed.startswith("$2"))
            # Salted SHA-256 hex digest is 64 characters long
            self.assertEqual(len(hashed), 64)
        finally:
            settings.jwt_secret = original_secret

    def test_l106_jwt_exception_propagation(self):
        from utils.security import decode_token, create_access_token
        from jose import jwt
        settings = get_settings()
        original_secret = settings.jwt_secret
        if not settings.jwt_secret:
            settings.jwt_secret = "test-secret-key-12345"
        try:
            # Test invalid token raises JWTError
            with self.assertRaises(jwt.JWTError):
                decode_token("invalid.token.here")

            # Test expired token raises ExpiredSignatureError
            from datetime import timedelta
            expired_token = create_access_token(data={"sub": "123"}, expires_delta=timedelta(seconds=-10))
            with self.assertRaises(jwt.ExpiredSignatureError):
                decode_token(expired_token)
        finally:
            settings.jwt_secret = original_secret


if __name__ == "__main__":
    unittest.main()

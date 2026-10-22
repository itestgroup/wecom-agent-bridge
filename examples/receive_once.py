"""Read-only inbox inspection. Does not print customer content, ACK, or send."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from clients.wecom_media import Client


if __name__ == '__main__':
    result = Client().inbox_messages(limit=5)
    print({'ok': result['ok'], 'count': len(result['messages']),
           'has_more': result['has_more'], 'ack_required': result['ack_required']})

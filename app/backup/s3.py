from app.errors import NotConfigured


class S3BackupTarget:
    def put(self, name, data):
        raise NotConfigured(
            "S3: faltan BACKUP_TARGET_URL, credenciales, BACKUP_ENCRYPTION_KEY y transporte en backup/s3.py"
        )

    def get(self, name):
        raise NotConfigured(
            "S3: faltan BACKUP_TARGET_URL, credenciales y transporte de restauración en backup/s3.py"
        )

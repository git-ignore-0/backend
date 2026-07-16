<?php

namespace App\Enums;

enum AccountStatus: string
{
    case PendingVerification = 'pending_verification';
    case Active = 'active';
    case Suspended = 'suspended';
    case DeletionRequested = 'deletion_requested';
    case Anonymized = 'anonymized';

    public function canAuthenticate(): bool
    {
        return in_array($this, [self::PendingVerification, self::Active], true);
    }
}

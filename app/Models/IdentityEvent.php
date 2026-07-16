<?php

namespace App\Models;

use Illuminate\Database\Eloquent\Concerns\HasUuids;
use Illuminate\Database\Eloquent\Model;

class IdentityEvent extends Model
{
    use HasUuids;

    protected $fillable = ['user_id', 'type', 'payload', 'status', 'attempts', 'available_at', 'processed_at'];

    protected function casts(): array
    {
        return [
            'payload' => 'array',
            'available_at' => 'datetime',
            'processed_at' => 'datetime',
        ];
    }
}

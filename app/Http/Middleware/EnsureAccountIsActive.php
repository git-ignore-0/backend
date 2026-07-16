<?php

namespace App\Http\Middleware;

use Closure;
use Illuminate\Http\Request;
use Symfony\Component\HttpFoundation\Response;

class EnsureAccountIsActive
{
    public function handle(Request $request, Closure $next): Response
    {
        if ($request->user() && ! $request->user()->isActive()) {
            if ($request->expectsJson() || $request->is('api/*')) {
                return response()->json([
                    'message' => 'The account is not active or the email address is not verified.',
                    'code' => 'account_inactive',
                ], 403);
            }

            return redirect()->route('verification.notice')
                ->with('status', 'Please verify your email address before continuing.');
        }

        return $next($request);
    }
}

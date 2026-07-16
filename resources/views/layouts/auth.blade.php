<!doctype html>
<html lang="{{ str_replace('_', '-', app()->getLocale()) }}">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="csrf-token" content="{{ csrf_token() }}">
    <title>@yield('title') · Natural Farming Vietnam</title>
    <link rel="stylesheet" href="{{ asset('css/auth.css') }}">
</head>
<body>
<main class="shell">
    <a class="brand" href="{{ route('account') }}">
        <span class="brand-mark">NFV</span>
        <span>Natural Farming Vietnam<small>Tài khoản chung</small></span>
    </a>

    <section class="card">
        @if (session('status'))
            <div class="notice">{{ session('status') }}</div>
        @endif

        @if ($errors->any())
            <div class="errors" role="alert">
                <strong>Vui lòng kiểm tra lại:</strong>
                <ul>@foreach ($errors->all() as $error)<li>{{ $error }}</li>@endforeach</ul>
            </div>
        @endif

        @yield('content')
    </section>

    <nav class="language" aria-label="Language">
        <a href="?lang=vi">Tiếng Việt</a><span>·</span><a href="?lang=en">English</a>
    </nav>
</main>
</body>
</html>

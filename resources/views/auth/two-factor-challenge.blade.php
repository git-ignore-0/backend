@extends('layouts.auth')
@section('title', 'Xác thực hai bước')
@section('content')
<h1>Xác thực hai bước</h1><p class="lead">Nhập mã từ ứng dụng xác thực hoặc mã khôi phục.</p>
<form method="POST" action="{{ route('two-factor.login') }}">@csrf<label>Mã xác thực<input name="code" inputmode="numeric" autocomplete="one-time-code" autofocus></label><label>Mã khôi phục<input name="recovery_code" autocomplete="one-time-code"></label><button type="submit">Tiếp tục</button></form>
@endsection

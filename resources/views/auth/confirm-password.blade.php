@extends('layouts.auth')
@section('title', 'Xác nhận mật khẩu')
@section('content')
<h1>Xác nhận mật khẩu</h1><p class="lead">Đây là khu vực bảo mật. Hãy nhập lại mật khẩu để tiếp tục.</p>
<form method="POST" action="{{ route('password.confirm') }}">@csrf<label>Mật khẩu<input name="password" type="password" required autocomplete="current-password"></label><button type="submit">Xác nhận</button></form>
@endsection

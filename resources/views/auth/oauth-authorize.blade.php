@extends('layouts.auth')
@section('title', 'Cho phép truy cập')
@section('content')
<h1>Cho phép truy cập?</h1>
<p class="lead"><strong>{{ $client->name }}</strong> muốn kết nối với tài khoản Natural Farming Vietnam của bạn.</p>
@if (count($scopes))<div class="permissions"><span>Ứng dụng sẽ được phép:</span><ul>@foreach ($scopes as $scope)<li>{{ $scope->description }}</li>@endforeach</ul></div>@endif
<div class="oauth-actions">
    <form method="POST" action="{{ route('passport.authorizations.approve') }}">@csrf<input type="hidden" name="auth_token" value="{{ $authToken }}"><button type="submit">Cho phép</button></form>
    <form method="POST" action="{{ route('passport.authorizations.deny') }}">@csrf @method('DELETE')<input type="hidden" name="auth_token" value="{{ $authToken }}"><button type="submit" class="secondary">Từ chối</button></form>
</div>
@endsection

class ClientUri:
    def __init__(self):
        self.uri_dict = {}
        # 群聊
        self.AddChatroomMember = '/api/Group/AddChatroomMember'
        self.GetChatRoomInfoDetail = '/api/Group/GetChatRoomInfoDetail'
        self.GetChatRoomInfo = '/api/Group/GetChatRoomInfo'
        self.GetChatRoomMemberDetail = '/api/Group/GetChatRoomMemberDetail'
        self.GetQRCode = '/api/Group/GetQRCode'
        self.InviteChatroomMember = '/api/Group/InviteChatroomMember'

        # 好友
        self.PassVerify = '/api/Friend/PassVerify'
        self.GetContractDetail = '/api/Friend/GetContractDetail'
        self.GetContractDetail = '/api/Friend/GetContractDetail'
        self.GetContractList = '/api/Friend/GetContractList'

        # 红包
        self.Qrydetailwxhb = '/api/TenPay/Qrydetailwxhb'

        # 登录
        self.AutoHeartBeatLog = '/api/Login/AutoHeartBeatLog'
        self.LoginGetQRx = '/api/Login/LoginGetQRx'
        self.LoginCheckQR = '/api/Login/LoginCheckQR'
        self.LogOut = '/api/Login/LogOut'
        self.LoginAwaken = '/api/Login/LoginAwaken'
        self.GetCacheInfo = '/api/Login/GetCacheInfo'
        self.HeartBeat = '/api/Login/HeartBeat'
        self.AutoHeartBeat = '/api/Login/AutoHeartBeat'
        self.CloseAutoHeartBeat = '/api/Login/CloseAutoHeartBeat'

        # 消息
        self.Revoke = '/api/Msg/Revoke'
        self.SendTxt = '/api/Msg/SendTxt'
        self.UploadImg = '/api/Msg/UploadImg'
        self.SendVideo = '/api/Msg/SendVideo'
        self.SendVoice = '/api/Msg/SendVoice'
        self.ShareLink = '/api/Msg/ShareLink'
        self.SendEmoji = '/api/Msg/SendEmoji'
        self.ShareCard = '/api/Msg/ShareCard'
        self.SendApp = '/api/Msg/SendApp'
        self.SendCDNFile = '/api/Msg/SendCDNFile'
        self.SendCDNImg = '/api/Msg/SendCDNImg'
        self.SendCDNVideo = '/api/Msg/SendCDNVideo'
        self.Sync = '/api/Msg/Sync'

        # 工具
        self.CdnDownloadImage = '/api/Tools/CdnDownloadImage'
        self.DownloadImg = '/api/Tools/DownloadImg'
        self.DownloadVoice = '/api/Tools/DownloadVoice'
        self.DownloadFile = '/api/Tools/DownloadFile'
        self.DownloadVideo = '/api/Tools/DownloadVideo'
        self.UpdateStepNumberApi = '/api/Tools/UpdateStepNumberApi'
        self.setproxy = '/api/Tools/setproxy'

        # 用户
        self.GetContractProfile = '/api/User/GetContractProfile'
        self.GetQRCode = '/api/User/GetQRCode'

    def pre_do(self):
        for key, value in self.__dict__.items():
            self.uri_dict[key] = value
        self.uri_dict.pop('uri_dict')
        return self

    def setBase(self, ip: str, port):
        for key, value in self.uri_dict.items():
            self.__dict__[key] = f'http://{ip}:{port}{self.uri_dict[key]}'


Uri = ClientUri().pre_do()

